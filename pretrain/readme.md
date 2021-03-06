
# About ABAE

[Download](https://drive.google.com/open?id=1L4LRi3BWoCqJt5h45J2GIAW9eP_zjiNc)
dataset and put it in `pretrain/dataset/*`.
Then Execute following command to train and evaluate ABAE.

```shell script
python abae.py --neg_count 20 \
    --aspect_size 14 \
    --data_dir dataset/restaurant \
    --save_path ./model/ABAE.pt
```

The author's original code:[ruidan/Unsupervised-Aspect-Extraction
](https://github.com/ruidan/Unsupervised-Aspect-Extraction).


# Training Strategy of UMPR

Specific pre-training methods for UMPR are listed below.

## 1. Pretraining of R-Net

Execute following command to generate pretraining parameters of R-Net. 
```shell script
python pretrain_rnet.py --data_dir ../data/music_small \
    --aspect_size 14 \
    --emb_dim 50 \
    --gru_size 64 \
    --save_ABAE ./model/trained_ABAE_rnet.pt \
    --save_rnet ./model/pretraining_rnet.pt
```
**Note**: `emb_dim` must be equal to 50 as same as word embedding size used in UMPR.

**Note**: `gru_size` should be equal to the parameter with same name of UMPR.

## 2. To be continued...
