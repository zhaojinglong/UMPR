import torch
import torchvision
from torch import nn


class RNet(nn.Module):

    def __init__(self, gru_in, gru_out):
        super(RNet, self).__init__()
        self.gru = nn.GRU(input_size=gru_in, hidden_size=gru_out, batch_first=True, bidirectional=True)
        self.M = nn.Parameter(torch.randn(2 * gru_out, 2 * gru_out))

    def forward(self, user_emb, item_emb):
        user_emb = user_emb.view(user_emb.shape[0], user_emb.shape[1] * user_emb.shape[2], user_emb.shape[3])
        item_emb = item_emb.view(item_emb.shape[0], item_emb.shape[1] * item_emb.shape[2], item_emb.shape[3])
        gru_u, hn = self.gru(user_emb)
        gru_i, hn = self.gru(item_emb)  # out(batch_size, sent_count * sent_length, 2*gru_out)
        A = gru_i @ self.M @ gru_u.transpose(-1, -2)
        soft_u = torch.softmax(torch.max(A, dim=-2).values, dim=-1)  # column
        soft_i = torch.softmax(torch.max(A, dim=-1).values, dim=-1)  # row. out(batch, sent_count * sent_length)
        atte_u = gru_u.transpose(-1, -2) @ soft_u.unsqueeze(-1)
        atte_i = gru_i.transpose(-1, -2) @ soft_i.unsqueeze(-1)  # shape(batch_size, 2*gru_out, 1)
        return gru_u.contiguous(), gru_i.contiguous(), soft_u, soft_i, atte_u.squeeze(-1), atte_i.squeeze(-1)


class SNet(nn.Module):

    def __init__(self, self_atte_size, repr_size):
        super(SNet, self).__init__()
        self.Ms = nn.Parameter(torch.randn(self_atte_size, repr_size))  # repr_size = 2u in the paper
        self.Ws = nn.Parameter(torch.randn(1, self_atte_size))

    def forward(self, gru_repr, word_soft, sent_length):
        # self-attention for sentence-level sentiment.
        batch_size = gru_repr.shape[0]
        sent_count = gru_repr.shape[1]//sent_length
        gru_repr = gru_repr.reshape(batch_size * sent_count, sent_length, -1).transpose(-1, -2)
        sent_soft = torch.softmax(self.Ws @ torch.tanh(self.Ms @ gru_repr), dim=-1)  # (temp_batch,1,r_length)
        self_atte = gru_repr @ sent_soft.transpose(-1, -2)  # out(temp_batch, repr_size, 1)

        sentiment_emb = word_soft.view(batch_size * sent_count, -1).sum(dim=-1, keepdim=True) * self_atte.squeeze(-1)
        sentiment_emb = sentiment_emb.view(batch_size, sent_count, -1).sum(dim=-2)
        return self_atte.view(batch_size, sent_count, -1), sentiment_emb  # output(batch, repr_size)


class CNet(nn.Module):

    def __init__(self, gru_in, gru_out, k_count, k_size, view_size, threshold=0.35):
        super(CNet, self).__init__()
        self.threshold = threshold

        self.gru = nn.GRU(input_size=gru_in, hidden_size=gru_out, batch_first=True, bidirectional=True)  # BiGRU
        self.cnn = nn.Sequential(
            # permute(0,2,1) -> (temp_bs, 2*gru_out, s_length)
            nn.Conv1d(in_channels=2 * gru_out, out_channels=k_count, kernel_size=k_size, padding=(k_size - 1) // 2),
            nn.ReLU(),
            # (temp_bs, k_count, s_length)
        )
        # max -> shape(temp_bs, k_count)
        # shape -> (batch_size, sent_count, k_count)
        self.linear = nn.Sequential(
            nn.Linear(k_count, view_size),
            nn.Sigmoid()
            # out(batch_size, sent_count, view_size)
        )

    def forward(self, review_emb):
        batch_size = review_emb.shape[0]
        sent_count = review_emb.shape[1]
        sent_length = review_emb.shape[2]
        gru_repr, hn = self.gru(review_emb.view(batch_size, sent_count * sent_length, -1))
        cnn_in = gru_repr.reshape(batch_size * sent_count, sent_length, -1).transpose(-1, -2)
        cnn_out = self.cnn(cnn_in)
        cnn_out = cnn_out.max(dim=-1)[0]  # (batch_size*sent_count, k_count)
        cnn_out = cnn_out.view(batch_size, sent_count, -1)

        linear_out = self.linear(cnn_out)
        # linear_out[linear_out < self.threshold] = 0  # 该句导致无法backward，只能用torch.where，原因不明
        linear_out = torch.where(linear_out < self.threshold, torch.zeros_like(linear_out), linear_out)
        final_repr = torch.sum(linear_out ** 2, dim=-2)  # out(batch_size, view_size)
        return gru_repr, linear_out, final_repr


class ReviewNet(nn.Module):

    def __init__(self, emb_size, gru_size, atte_size):
        super(ReviewNet, self).__init__()
        self.r_net = RNet(emb_size, gru_size)  # Note: using Bi-GRU
        self.s_net_u = SNet(atte_size, gru_size * 2)
        self.s_net_i = SNet(atte_size, gru_size * 2)

        self.linear_u = nn.Linear(gru_size * 4, gru_size * 2, bias=False)
        self.linear_i = nn.Linear(gru_size * 4, gru_size * 2, bias=False)

    def forward(self, user_emb, item_emb):
        u_s_length = user_emb.shape[-2]
        i_s_length = item_emb.shape[-2]

        gru_u, gru_i, soft_u, soft_i, atte_u, atte_i = self.r_net(user_emb, item_emb)
        _, sentiment_u = self.s_net_u(gru_u, soft_u, u_s_length)
        _, sentiment_i = self.s_net_i(gru_i, soft_i, i_s_length)

        # Textual Matching
        repr_u = torch.cat([atte_u, sentiment_u], dim=-1)  # formula (7)
        repr_i = torch.cat([atte_i, sentiment_i], dim=-1)
        represent = torch.tanh(self.linear_u(repr_u) + self.linear_i(repr_i))  # formula (8)
        return represent  # output shape(batch, 2u) where u is GRU hidden size


class ControlNet(nn.Module):
    def __init__(self, emb_size, gru_size, k_count, k_size, view_size, threshold, atte_size):
        super(ControlNet, self).__init__()
        self.c_net = CNet(emb_size, gru_size, k_count, k_size, view_size, threshold)
        self.s_net = SNet(atte_size, repr_size=gru_size * 2)
        self.ss_net = nn.Sequential(
            nn.Linear(gru_size * 2, 1),
            nn.Sigmoid()
        )

    def forward(self, user_emb, item_emb, ui_emb):
        ui_s_length = ui_emb.shape[-2]

        gru_repr, view_possibility, c_net_out = self.c_net(ui_emb)
        _, _, c_u = self.c_net(user_emb)
        _, _, c_i = self.c_net(item_emb)
        s, _ = self.s_net(gru_repr, view_possibility, ui_s_length)
        senti_score = self.ss_net(s)  # out(batch_size, s_count, 1)
        view_score = (senti_score * (view_possibility ** 2)).sum(dim=-2) / (view_possibility ** 2).sum(dim=-2)  # (17)
        q_p = torch.zeros_like(view_score)
        q_pos = 4 * (view_score - 0.5) ** 2
        q_neg = 4 * (0.5 - view_score) ** 2
        q_p[view_score > 0.5] = 1
        q_pos[view_score < 0.5] = 0
        q_neg[view_score > 0.5] = 0

        prefer_pos = c_net_out * q_p * q_pos
        prefer_neg = c_net_out * (1 - q_p) * q_neg
        return c_u, c_i, prefer_pos, prefer_neg  # (batch_size, view_size)


class VisualNet(nn.Module):
    def __init__(self, view_size):
        super().__init__()
        self.vgg16 = torchvision.models.vgg16(pretrained=True)
        vgg_out = 1000  # 1000 is the value that vgg16 output.
        self.pos_v_emb = nn.Parameter(torch.randn(vgg_out))
        self.neg_v_emb = nn.Parameter(torch.randn(vgg_out))
        # The out_size ought be 1. But real view_size of photos is only 1. So let out_size be view_size to increase it.
        # self.linear = nn.Linear(vgg_out, 1)  # According to original paper.
        self.linear = nn.Linear(vgg_out, view_size)

    def forward(self, images, c_u, c_i):
        batch_size = images.shape[0]
        photo_count = images.shape[1]
        images = images.view(torch.Size([images.shape[0] * images.shape[1]]) + images.shape[2:])  # (-1,C,H,W)
        img_repr = self.vgg16(images)  # (-1,1000).
        img_repr = img_repr.view(batch_size, photo_count, -1)  # (b,pc,1000). Original paper:(b,view_size,pc,1000)
        # According to paper, calculate average for pictures of each view.
        img_repr = img_repr.mean(dim=-2)  # eq.(10) (b,1000). Original paper:(b,view_size,1000)

        pos_match = torch.tanh(torch.abs(self.linear(self.pos_v_emb) - self.linear(img_repr)))  # eq.(11) \tilde{x^V+}
        neg_match = torch.tanh(torch.abs(self.linear(self.neg_v_emb) - self.linear(img_repr)))  # (b,view_size)

        final_pos = c_u * c_i * (1 - pos_match)
        final_neg = c_u * c_i * (1 - neg_match)

        return pos_match, neg_match, final_pos, final_neg


class UMPR(nn.Module):
    def __init__(self, config, word_emb):
        super(UMPR, self).__init__()
        self.loss_v_rate = config.loss_v_rate
        self.embedding = nn.Embedding.from_pretrained(torch.Tensor(word_emb))

        self.review_net = ReviewNet(self.embedding.embedding_dim, config.gru_size, config.self_atte_size)
        self.control_net = ControlNet(self.embedding.embedding_dim, config.gru_size, config.kernel_count,
                                      config.kernel_size, config.view_size, config.threshold, config.self_atte_size)
        self.visual_net = VisualNet(config.view_size)

        self.linear_fusion = nn.Sequential(
            nn.Linear(config.gru_size * 2 + config.view_size + config.view_size, 1),
            nn.ReLU()
        )

    def forward(self, user_reviews, item_reviews, ui_reviews, photos, labels):
        user_emb = self.embedding(user_reviews)  # (batch_size, sent_count, sent_length, emb_size)
        item_emb = self.embedding(item_reviews)
        ui_emb = self.embedding(ui_reviews)

        review_net_repr = self.review_net(user_emb, item_emb)  # (batch, 2u) where u is GRU hidden size
        c_u, c_i, prefer_pos, prefer_neg = self.control_net(user_emb, item_emb, ui_emb)
        pos_match, neg_match, final_pos, final_neg = self.visual_net(photos, c_u, c_i)  # (b,view_size)

        prediction = self.linear_fusion(torch.cat([review_net_repr, final_pos, final_neg], dim=-1)).squeeze(-1)
        loss_v = torch.mean(prefer_pos.transpose(-1, -2) @ pos_match + prefer_neg.transpose(-1, -2) @ neg_match)  # eq20
        loss_r = torch.nn.functional.mse_loss(prediction, labels, reduction='mean')
        loss = loss_r + loss_v * self.loss_v_rate
        return prediction, loss
