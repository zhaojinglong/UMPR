UMPR
===
Implementation for the paper：  
>Xu, Cai, Ziyu Guan, Wei Zhao, Quanzhou Wu, Meng Yan, Long Chen, and Qiguang Miao.
 "Recommendation by Users' Multi-modal Preferences for Smart City Applications."
 IEEE Transactions on Industrial Informatics (2020).

# Environments
+ python 3.8
+ pytorch 1.7

# Dataset and Word Embedding

```
UMPR
│
├─data
│  │
│  ├─amazonCSJ
│  │       reviews_Clothing_Shoes_and_Jewelry.json.gz
│  │       meta_Clothing_Shoes_and_Jewelry.json.gz
│  │
│  ├─music
│  │       reviews_Digital_Music.json.gz
│  │       meta_Digital_Music.json.gz
│  │
│  └─yelp
│     │    yelp_academic_dataset_review.json
│     │    photos.json
│     │
│     └─photos
│              *.jpg
│
└─embedding
           glove.6B.50d.txt
           punctuations.txt
           stopwords.txt
```

+ Dataset Amazon(2014) http://jmcauley.ucsd.edu/data/amazon/links.html
+ Dataset Yelp(2020) https://www.yelp.com/dataset
+ Word Embedding https://nlp.stanford.edu/projects/glove

# Running

1. Execute `data_process.py` to generate 
`train.csv`,`valid.csv`,`test.csv`,`photos.json`.
```shell script
python data/data_process.py --data_type amazon \
    --data_path ./data/music/reviews_Digital_Music.json.gz \
    --meta_path ./data/music/meta_Digital_Music.json.gz \
    --save_dir ./data/music \
    --train_rate 0.8
```

2. For amazon(**not yelp**), execute `down_photos.py` to download `photos/*.jpg`.
```shell script
python data/down_photos.py --photos_json ./data/music/photos.json
```

3. Train and evaluate the model.  
```shell script
python main.py --data_dir ./data/music --view_size 1
```
```shell script
python main.py --data_dir ./data/yelp --view_size 5
```

4. Test only:
```shell script
python test.py --data_dir ./data/music --view_size 1 --model_path ./model/default.pt
```
```shell script
python test.py --data_dir ./data/yelp --view_size 5 --model_path ./model/default.pt
```

# Experiment

<p align="center" style="margin: 0">
Table 1. 
Performance comparison (mean squared error) on several datasets.
</p>
<table align="center">
    <tr>
        <th>Dataset(number of reviews)</th>
        <th>MF</th>
        <th>NeulMF</th>
        <th>DeepCoNN</th>
        <th>TransNets</th>
        <th>MPCN</th>
        <th>UMPR-R</th>
        <th>UMPR</th>
    </tr>
    <tr>
        <td>Amazon Music small (64,706)</td>
        <td>0.900899</td>
        <td>0.822472</td>
        <td>-</td>
        <td>-</td>
        <td>-</td>
        <td>1.117017</td>
        <td>0.925538</td>
    </tr>
    <tr>
        <td>Amazon Music (836,006)</td>
        <td>0.875224</td>
        <td>0.825261</td>
    </tr>
    <tr>
        <td>Amazon Clothing, Shoes and Jewelry (5,748,920)</td>
        <td>1.512551</td>
        <td>1.502135</td>
    </tr>
    <tr>
        <td>Yelp (8,021,121)</td>
        <td>2.171064</td>
        <td>2.041674</td>
    </tr>
</table>

**MF**: General Matrix Factorization.
[Details](https://github.com/iamwinter/MatrixFactorization)

**NeuMF**: Neural Collaborative Filtering.
[Details](https://github.com/iamwinter/NeuralCollaborativeFiltering)

**DeepCoNN**: [Details](https://github.com/iamwinter/DeepCoNN)

**UMPR-R**: only review network part of UMPR.

**UMPR**: Our complete model.


# Experiment Notebook

+ 2021.03.22

  - 发现`torchvision.models.models.vgg16`最后一层是`Linear`，输出值很大（~1e6）。
  - 公式18涉及除法，有可能造成除以0的情况。解决：分母额外加1e-6。

+ 2021.03.24

  - 数据集处理技巧：评论的句子按长度排序，长句优先用于训练，短句则大概率在被对齐句数时丢弃。
  - GRU改为不定长输入。原方法是输入等长序列。

+ 2021.03.26

  - 对于图片集，一次性读入因内存不足而退出。  
  解决：Dataset只存储图片路径，每个batch训练/测试时即时从磁盘中读取图片。

+ 2021.03.28

  - 尝试yelp数据集时，内存不足。  
    解决：把sentence语句保存到“语句池”，统计语句时提取语句，
    此时python只会得到其引用，从而节省内存

+ 2021.03.29

  - 多GPU训练时，不定长GRU潜在的**巨坑**：使用pytorch的DataParallel实现多GPU训练时，
    它会将一个batch的数据均分输入到多个GPU上，
    于是不定长GRU的输入`lengths`只是一个batch中的一部分，
    **整个batch上的最大length值可能并不出现在当前GPU的`lengths`中**，但是序列（GRU的`input`）
    却早已被pad为最大长度，即`lengths`最大值小于实际pad后的序列长度。
    然后，`nn.utils.rnn.pack_padded_sequence`是依据`lengths`来pack的，
    所以最后`ImprovedRnn`的输出tensor中`序列长度那一个维度`就被缩短了，
    从而引起后续计算报维度不匹配的错误！  
    解决：在GRU之后，执行`nn.utils.rnn.pad_packed_sequence`时设置参数`total_length`
    为最大长度！
    
  - 用`DataParallel`进行多GPU训练报一个警告：`/torch/nn/parallel/_functions.py:64: 
    UserWarning: Was asked to gather along dimension 0, 
    but all input tensors were scalars; 
    will instead unsqueeze and return a vector.`
    原因是网络的输出`loss`是个标量，多个GPU输出的`loss`合并时，只能将这些标量合并为向量。  
    解决：我删掉了源码中这句警告语句。