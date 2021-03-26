import os
import sys
import time
import logging
from concurrent.futures import as_completed
from concurrent.futures.thread import ThreadPoolExecutor

import numpy
from PIL import Image
import pandas as pd
import torch
from torch.nn import functional as F


def get_logger(log_file=None, file_level=logging.INFO, stdout_level=logging.DEBUG, logger_name=__name__):
    logging.root.setLevel(0)
    formatter = logging.Formatter('%(asctime)s %(levelname)5s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    _logger = logging.getLogger(logger_name)

    if log_file is not None:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(level=file_level)
        file_handler.setFormatter(formatter)
        _logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(level=stdout_level)
    stream_handler.setFormatter(formatter)
    _logger.addHandler(stream_handler)
    return _logger


def date(f='%Y-%m-%d %H:%M:%S'):
    return time.strftime(f, time.localtime())


def process_bar(current, total, prefix='', auto_rm=True):
    bar = '=' * int(current / total * 50)
    bar = f' {prefix} |{bar.ljust(50)}| ({current}/{total}) {current / total:.1%} | '
    print(bar, end='\r', flush=True)
    if auto_rm and current == total:
        print(end=('\r' + ' ' * len(bar) + '\r'), flush=True)


def load_embedding(word2vec_file):
    with open(word2vec_file, encoding='utf-8') as f:
        word_emb = list()
        word_dict = dict()
        word_emb.append([0])
        word_dict['<UNK>'] = 0
        for line in f.readlines():
            tokens = line.split(' ')
            word_emb.append([float(i) for i in tokens[1:]])
            word_dict[tokens[0]] = len(word_dict)
        word_emb[0] = [0] * len(word_emb[1])
    return word_emb, word_dict


def load_photos(photos_dir, resize=(224, 224), max_workers=36):
    paths = []
    for name in os.listdir(photos_dir):
        path = os.path.join(photos_dir, name)
        if os.path.isfile(path) and name.endswith('jpg'):
            paths.append(path)

    def load_image(img_path):
        try:
            image = Image.open(img_path).convert('RGB').resize(resize)
            image = numpy.asarray(image) / 255
            return os.path.basename(img_path)[:-4], image.transpose((2, 0, 1))
        except Exception:
            return img_path, None  # Damaged picture: {img_path}

    pool = ThreadPoolExecutor(max_workers=max_workers)  # to read {len(paths)} pictures
    tasks = [pool.submit(load_image, path) for path in paths]

    photos_dict = dict()
    damaged = []
    for i, task in enumerate(as_completed(tasks)):
        name, photo = task.result()
        if photo is not None:
            photos_dict[name] = photo
        else:
            damaged.append(name)
        process_bar(i + 1, len(tasks), prefix='Loading photos')

    for name in damaged:
        print(f'## Failed to open {name}.jpg')
    return photos_dict


def predict_mse(model, dataloader):
    device = next(model.parameters()).device
    mse, sample_count = 0, 0
    with torch.no_grad():
        model.eval()
        for i, batch in enumerate(dataloader):
            cur_batch = map(lambda x: x.to(device), batch)
            user_reviews, item_reviews, reviews, u_lengths, i_lengths, ui_lengths, photos, ratings = cur_batch
            pred, loss = model(user_reviews, item_reviews, reviews, u_lengths, i_lengths, ui_lengths, photos, ratings)
            mse += F.mse_loss(pred, ratings, reduction='sum').item()
            sample_count += len(ratings)
            process_bar(i + 1, len(dataloader), prefix='Evaluate')
    return mse / sample_count


def pad_list(arr, dim1, dim2, pad_elem=0):  # 二维list调整长宽，截长补短
    arr = arr[:dim1] + [[pad_elem] * dim2] * (dim1 - len(arr))  # dim 1
    arr = [r[:dim2] + [pad_elem] * (dim2 - len(r)) for r in arr]  # dim 2
    return arr


class Dataset(torch.utils.data.Dataset):
    def __init__(self, data_path, photo_json, word_dict, config):
        self.word_dict = word_dict
        self.s_count = config.sent_count
        self.ui_s_count = config.ui_sent_count
        self.s_length = config.sent_length
        self.lowest_s_count = config.lowest_sent_count  # lowest amount of sentences wrote by exactly one user/item
        self.PAD_WORD_idx = word_dict[config.PAD_WORD]
        self.photo_count = config.photo_count

        df = pd.read_csv(data_path)
        df['review'] = df['review'].apply(self._cut_review)
        self.retain_idx = [True] * len(df)  # Save the indices of empty samples, delete them at last.
        user_reviews = self._get_reviews(df)  # Gather reviews for every user without target review(i.e. u for i).
        item_reviews = self._get_reviews(df, 'item_num', 'user_num')
        ui_reviews = self._get_ui_review(df['review'])
        photos_name = self._get_photos_name(photo_json, df['itemID'], config.view_size)

        self.data = (
            [v for v, r in zip(user_reviews, self.retain_idx) if r],
            [v for v, r in zip(item_reviews, self.retain_idx) if r],
            [v for v, r in zip(ui_reviews, self.retain_idx) if r],
            [v for v, r in zip(photos_name, self.retain_idx) if r],
            [v for v, r in zip(df['rating'], self.retain_idx) if r],
        )

    def __getitem__(self, idx):
        return tuple(x[idx] for x in self.data)

    def __len__(self):
        return len(self.data[0])

    def _get_reviews(self, df, lead='user_num', costar='item_num', max_workers=36):
        # For every sample(user,item), gather reviews for user/item.
        reviews_by_lead = dict(list(df[[costar, 'review']].groupby(df[lead])))  # Information for every user/item

        def gather_review(idx, lead_id, costar_id):
            df_data = reviews_by_lead[lead_id]  # get information of lead, return DataFrame.
            reviews = df_data['review'][df_data[costar] != costar_id]  # get reviews without review u for i.
            sentences = [sent[:self.s_length] for r in reviews for sent in r]  # cut too long sentence!
            if len(sentences) < self.lowest_s_count:
                self.retain_idx[idx] = False
            sentences.sort(key=lambda x: -len(x))  # sort by length of sentence.
            sentences = sentences[:self.s_count] + [list()] * (self.s_count - len(sentences))  # Adjust number of sentences!
            # sentences = pad_list(sentences, self.s_count, self.s_length)  # pad!
            return idx, sentences

        pool = ThreadPoolExecutor(max_workers=max_workers)
        tasks = [pool.submit(gather_review, i, x[0], x[1]) for i, x in enumerate(zip(df[lead], df[costar]))]

        ret_sentences = [list()] * len(tasks)
        for i, task in enumerate(as_completed(tasks)):
            idx, sents = task.result()
            ret_sentences[idx] = sents
            process_bar(i + 1, len(tasks), prefix=f'Loading sentences of {lead}')
        return ret_sentences

    def _get_ui_review(self, reviews: pd.Series):
        reviews = reviews.to_list()
        for i, sentences in enumerate(reviews):
            sentences.sort(key=lambda x: -len(x))  # sort by length of sentence.
            sentences = sentences[:self.ui_s_count] + [list()] * (self.ui_s_count - len(sentences))  # Adjust number
            sentences = [sent[:self.s_length] for sent in sentences]  # cut too long sentence!
            # sentences = pad_list(sentences, self.ui_s_count, self.s_length)
            reviews[i] = sentences
        return reviews

    def _cut_review(self, review):  # Split a sentence into words, and map each word to a unique number by dict.
        try:
            sentences = review.strip().split('.')
            for i in range(len(sentences)):
                sentences[i] = [self.word_dict.get(w, self.PAD_WORD_idx) for w in sentences[i].split()]
            return sentences
        except Exception:
            return [list()]

    def _get_photos_name(self, photos_json, item_id_list, view_size, max_workers=36):
        photo_df = pd.read_json(photos_json, orient='records', lines=True)
        if 'label' not in photo_df.columns:
            photo_df['label'] = 'None'  # Due to amazon have no label.
        label_index = dict([(label, i) for i, label in enumerate(photo_df['label'].drop_duplicates())])  # label: index
        if len(label_index) != view_size:
            print(f'Number of labels in photos.json is {len(label_index)}! Set Config().view_size={len(label_index)}!')
            exit(1)

        photos_by_item = dict(list(photo_df[['photo_id', 'label']].groupby(photo_df['business_id'])))  # iid: df

        def get_photo_info(idx, iid):
            item_df = photos_by_item.get(iid, pd.DataFrame(columns=['photo_id', 'label']))  # all photos of this item.
            pid_by_label = dict(list(item_df['photo_id'].groupby(item_df['label'])))
            item_photos = [list()] * len(label_index)
            for label, label_idx in label_index.items():
                pids = pid_by_label.get(label, pd.Series()).to_list()
                if len(pids) < 1:
                    self.retain_idx[idx] = False
                pids = pids[:self.photo_count] + ['unk_name'] * (self.photo_count - len(pids))
                # pids = [self.image_dict.get(name, numpy.zeros(self.photo_size)) for name in pids]  # 直接加载为图片
                item_photos[label_idx] = pids
            return idx, item_photos

        pool = ThreadPoolExecutor(max_workers=max_workers)
        tasks = [pool.submit(get_photo_info, idx, iid) for idx, iid in enumerate(item_id_list)]

        photos_name = [list()] * len(tasks)
        for i, task in enumerate(as_completed(tasks)):
            idx, i_photos = task.result()
            photos_name[idx] = i_photos
            process_bar(i + 1, len(tasks), prefix=f'Loading photos')
        return photos_name


def get_image(name, photo_dir, resize=(224, 224)):
    path = os.path.join(photo_dir, name + 'jpg')
    try:
        image = Image.open(path).convert('RGB').resize(resize)
        image = numpy.asarray(image) / 255
        return image.transpose((2, 0, 1))
    except Exception:
        return numpy.zeros([3] + list(resize))  # default


def batch_loader(batch_list, photo_dir, photo_size=(224, 224), pad_value=0):
    data = [list() for i in batch_list[0]]
    lengths = [list() for i in range(3)]  # length of Ru/Ri/Rui
    max_lengths = [0, 0, 0]  # Ru/Ri/Rui
    for sample in batch_list:
        for i, val in enumerate(sample):
            if i in (0, 1, 2):  # reviews
                lengths[i].append([max(1, len(s)) for s in val])
                max_lengths[i] = max(max_lengths[i], max(lengths[i][-1]))
                data[i].append(val)
            if i == 3:  # photos
                data[i].append([[get_image(name, photo_dir, photo_size) for name in ps] for ps in val])
            if i == 4:  # ratings
                data[i].append(val)

    for i, batch_reviews in enumerate(data):
        for x, sents in enumerate(batch_reviews):  # sents is a list of sentences per sample.
            for y, s in enumerate(sents):
                data[i][x][y] = s + [pad_value] * (max_lengths[i] - len(s))
        if i == 2:
            break

    return (
        torch.LongTensor(data[0]),
        torch.LongTensor(data[1]),
        torch.LongTensor(data[2]),
        torch.LongTensor(lengths[0]),
        torch.LongTensor(lengths[1]),
        torch.LongTensor(lengths[2]),
        torch.Tensor(data[3]),
        torch.Tensor(data[4]),
    )
