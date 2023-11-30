import time
from GraphDataset.data import get_dataset
import utils
import random
import argparse
import numpy as np
import torch
import torch.nn.functional as F
from early_stop import EarlyStopping, Stop_args
from model import TransformerModel
from lr import PolynomialDecayLR
import os.path
import torch.utils.data as Data
import argparse
import networkx as nx
import pandas as pd


# Training settings
def parse_args():
    """
    Generate a parameters parser.
    """
    # parse parameters
    parser = argparse.ArgumentParser()

    # main parameters
    parser.add_argument('--name', type=str, default=None)
    parser.add_argument('--dataset', type=str, default='pubmed',
                        help='Choose from {pubmed}')
    parser.add_argument('--device', type=int, default=1, 
                        help='Device cuda id')
    parser.add_argument('--seed', type=int, default=3407, 
                        help='Random seed.')

    # model parameters
    parser.add_argument('--t_nums', type=int, default=20,
                        help='nums of token_paths')
    parser.add_argument('--w_len', type=int, default=20,
                        help='max walk_length of token_path')
    parser.add_argument('--pe_dim', type=int, default=15,
                        help='position embedding size')
    parser.add_argument('--hidden_dim', type=int, default=512,
                        help='Hidden layer size')
    parser.add_argument('--ffn_dim', type=int, default=64,
                        help='FFN layer size')
    parser.add_argument('--n_layers', type=int, default=1,
                        help='Number of Transformer layers')
    parser.add_argument('--n_heads', type=int, default=8,
                        help='Number of Transformer heads')
    parser.add_argument('--dropout', type=float, default=0.1,
                        help='Dropout')
    parser.add_argument('--attention_dropout', type=float, default=0.1,
                        help='Dropout in the attention layer')

    # training parameters
    parser.add_argument('--batch_size', type=int, default=1000,
                        help='Batch size')
    parser.add_argument('--epochs', type=int, default=2000,
                        help='Number of epochs to train.')
    parser.add_argument('--tot_updates',  type=int, default=1000,
                        help='used for optimizer learning rate scheduling')
    parser.add_argument('--warmup_updates', type=int, default=400,
                        help='warmup steps')
    parser.add_argument('--peak_lr', type=float, default=0.001, 
                        help='learning rate')
    parser.add_argument('--end_lr', type=float, default=0.0001, 
                        help='learning rate')
    parser.add_argument('--weight_decay', type=float, default=0.00001,
                        help='weight decay')
    parser.add_argument('--uniformRWRate', type=float, default=0.25,
                        help='rate for uniform random walk')
    parser.add_argument('--nonBackRWRate', type=float, default=0.25,
                        help='rate for non-backtracking random walk')
    parser.add_argument('--nJumpRate', type=float, default=0.25,
                        help='rate for neighborhood jump walk')
    parser.add_argument('--patience', type=int, default=50, 
                        help='Patience for early stopping')
    parser.add_argument('--sgpm_token', action="store_true", 
                        help='use sgpm token or not')
    parser.add_argument('--hop_num', type=int, default=3, 
                        help='number of hop token')

    return parser.parse_args()

args = parse_args()


device = torch.device('cuda' if torch.cuda.is_available() else 'cpu') # or args.device

print(device)

random.seed(args.seed)
np.random.seed(args.seed)
torch.manual_seed(args.seed)
if torch.cuda.is_available():
    torch.cuda.manual_seed(args.seed)

# Load and pre-process data
G, features, labels, idx_train, idx_val, idx_test = get_dataset(args.dataset, 60)

# pre_process to get random_walk
print('pre_process to generate Random Walk Path')
path = "./DatasetPathInfo/" + args.dataset + "/" + args.dataset + '_num=' + str(args.t_nums) + '_len=' + str(args.w_len) + '_uniformRWRate=' + str(args.uniformRWRate) + '_nonBackRWRate=' + str(args.nonBackRWRate) + '_nJumpRate=' + str(args.nJumpRate) + '.pt'
if not os.path.isfile(path):
    utils.mixed_walk_gen(G, args.t_nums, args.w_len, args.dataset, seed=42, uniformRWRate=args.uniformRWRate, nonBackRWRate=args.nonBackRWRate, nJumpRate=args.nJumpRate)

print("sgpm token exist? ", args.sgpm_token)
processed_features = utils.get_token(G, features, args.t_nums, args.w_len, args.dataset, args.sgpm_token, args.hop_num, args.uniformRWRate, args.nonBackRWRate, args.nJumpRate)

labels = labels.to(device) 
batch_data_train = Data.TensorDataset(processed_features[idx_train], labels[idx_train])
batch_data_val = Data.TensorDataset(processed_features[idx_val], labels[idx_val])
batch_data_test = Data.TensorDataset(processed_features[idx_test], labels[idx_test])

train_data_loader = Data.DataLoader(batch_data_train, batch_size=args.batch_size, shuffle = True)
val_data_loader = Data.DataLoader(batch_data_val, batch_size=args.batch_size, shuffle = True)
test_data_loader = Data.DataLoader(batch_data_test, batch_size=args.batch_size, shuffle = True)

if args.sgpm_token == False:
    numTokens = args.t_nums + 1 + args.hop_num
else:
    numTokens = args.t_nums + 1 +1 + args.hop_num

# model configuration
model = TransformerModel(t_nums=numTokens, 
                        n_class=labels.max().item() + 1, 
                        # input_dim=features.shape[1] * (args.w_len*2+1), 
                        input_dim=features.shape[1] * 2, 
                        pe_dim = args.pe_dim,
                        n_layers=args.n_layers,
                        num_heads=args.n_heads,
                        hidden_dim=args.hidden_dim,
                        ffn_dim=args.ffn_dim,
                        dropout_rate=args.dropout,
                        attention_dropout_rate=args.attention_dropout).to(device)

print(model)
print('total params:', sum(p.numel() for p in model.parameters()))

optimizer = torch.optim.AdamW(model.parameters(), lr=args.peak_lr, weight_decay=args.weight_decay)
lr_scheduler = PolynomialDecayLR(
                optimizer,
                warmup_updates=args.warmup_updates,
                tot_updates=args.tot_updates,
                lr=args.peak_lr,
                end_lr=args.end_lr,
                power=1.0,
            )


def train_valid_epoch(epoch):
    
    model.train()
    loss_train_b = 0
    acc_train_b = 0
    for _, item in enumerate(train_data_loader):
        nodes_features = item[0].to(device)
        labels = item[1].to(device)

        optimizer.zero_grad()
        output = model(nodes_features)
        
        loss_train = F.nll_loss(output, labels)
        loss_train.backward()
        optimizer.step()
        lr_scheduler.step()

        loss_train_b += loss_train.item()
        acc_train = utils.accuracy_batch(output, labels)
        acc_train_b += acc_train.item()
        
    
    model.eval()
    loss_val = 0
    acc_val = 0
    for _, item in enumerate(val_data_loader):
        nodes_features = item[0].to(device)
        labels = item[1].to(device)

        output = model(nodes_features)
        loss_val += F.nll_loss(output, labels).item()
        acc_val += utils.accuracy_batch(output, labels).item()
        

    print('Epoch: {:04d}'.format(epoch+1),
        'loss_train: {:.4f}'.format(loss_train_b),
        'acc_train: {:.4f}'.format(acc_train_b/len(idx_train)),
        'loss_val: {:.4f}'.format(loss_val),
        'acc_val: {:.4f}'.format(acc_val/len(idx_val)))

    return loss_val, acc_val



def test():

    loss_test = 0
    acc_test = 0
    for _, item in enumerate(test_data_loader):
        nodes_features = item[0].to(device)
        labels = item[1].to(device)


        model.eval()

        output = model(nodes_features)
        loss_test += F.nll_loss(output, labels).item()
        acc_test += utils.accuracy_batch(output, labels).item()

    
    train_loss = "{:.4f}".format(loss_test)
    train_accuracy = "{:.4f}".format(acc_test/len(idx_test))
    result = ("Test set results:",
        "loss= {:.4f}".format(loss_test),
        "accuracy= {:.4f}".format(acc_test/len(idx_test)))

    print(result)
    return train_loss, train_accuracy



t_total = time.time()
stopping_args = Stop_args(patience=args.patience, max_epochs=args.epochs)
early_stopping = EarlyStopping(model, **stopping_args)
for epoch in range(args.epochs):
    loss_val, acc_val = train_valid_epoch(epoch)
    if early_stopping.check([acc_val, loss_val], epoch):
        break


train_cost = "{:.4f}s".format(time.time() - t_total)

# Restore best model
loading_epoch = early_stopping.best_epoch+1

print("Optimization Finished!")
print("Train cost: {:.4f}s".format(time.time() - t_total))
print('Loading {}th epoch'.format(early_stopping.best_epoch+1))
model.load_state_dict(early_stopping.best_state)

train_loss, train_accuracy = test()

#记录loss和accuracy
filename = args.dataset + '_test_result.csv'

# df = pd.DataFrame(columns=['token_nums','walk_length','time', 'epoch','train Loss','training accuracy','n_heads'])#列名
 
    
# new_data = pd.DataFrame(
#     [[args.t_nums, args.w_len, train_cost, loading_epoch, train_loss, train_accuracy, args.n_heads]], 
#     columns=['token_nums','walk_length','time', 'epoch','train Loss','training accuracy','n_heads']
# )





df = pd.DataFrame(columns=['SGPM-token', 'path-token_nums', 'walk_length', 'time', 'hidden_dim', 'parameters', 'hop_num', 'uniformRWRate', 'nonBackRWRate', 'nJumpRate', 'lr', 'weight_decay', 'n_heads', 'epoch', 'train Loss', 'seed', 'training accuracy'])#列名
 
    
new_data = pd.DataFrame(
    [[args.sgpm_token, args.t_nums, args.w_len, train_cost, args.hidden_dim, sum(p.numel() for p in model.parameters()), args.hop_num, args.uniformRWRate, args.nonBackRWRate, args.nJumpRate, args.peak_lr, args.weight_decay, args.n_heads, loading_epoch, train_loss, args.seed ,train_accuracy]], 
    columns=['SGPM-token', 'path-token_nums', 'walk_length', 'time', 'hidden_dim', 'parameters', 'hop_num', 'uniformRWRate', 'nonBackRWRate', 'nJumpRate', 'lr', 'weight_decay', 'n_heads', 'epoch', 'train Loss', 'seed', 'training accuracy']
)
    

if os.path.isfile(filename):
    new_data.to_csv(filename, mode='a', header=False, index=False)
else:
    new_data.to_csv(filename, mode='w', header=True, index=False)