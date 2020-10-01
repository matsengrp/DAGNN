
from torchvision import transforms
from tqdm import tqdm
import argparse
import numpy as np
import torch.multiprocessing
torch.multiprocessing.set_sharing_strategy('file_system')
### importing OGB
from ogb.graphproppred import PygGraphPropPredDataset, Evaluator

### importing utils
from utils import get_vocab_mapping
### for data transform
from utils import augment_edge, encode_y_to_arr, decode_arr_to_seq

### DAGNN
import random
from models.baselines import *
from src.constants import *
from src.tg.dataloader import DataLoader
from src.tg.data_parallel import DataParallel
###
import pandas as pd
# make sure summary_report is imported after src.utils (also from dependencies)
from utils2 import *

multicls_criterion = torch.nn.CrossEntropyLoss()


def eval(model, device, loader, evaluator, arr_to_seq):
    model.eval()
    seq_ref_list = []
    seq_pred_list = []

    for step, batch in enumerate(tqdm(loader, desc="Iteration")):
        # batch = batch.to(device)

        batch = [b for b in batch if not b.x.shape[0] == 1]
        if not batch:  #if batch.x.shape[0] == 1:
            pass
        else:
            with torch.no_grad():
                pred_list = model(batch)

            mat = []
            for i in range(len(pred_list)):
                mat.append(torch.argmax(pred_list[i], dim = 1).view(-1,1))
            mat = torch.cat(mat, dim = 1)
            
            seq_pred = [arr_to_seq(arr) for arr in mat]
            
            # PyG = 1.4.3
            # seq_ref = [batch.y[i][0] for i in range(len(batch.y))]

            # PyG >= 1.5.0
            seq_ref = [b.y[i] for b in batch for i in range(len(b.y))] # [batch.y[i] for i in range(len(batch.y))]
            # print(seq_pred)
            # print(seq_ref)
            # print([s for s in seq_ref[0] if s in vocab2index])
            # print("*"*20)
            seq_ref_list.extend(seq_ref)
            seq_pred_list.extend(seq_pred)

    input_dict = {"seq_ref": seq_ref_list, "seq_pred": seq_pred_list}

    return evaluator.eval(input_dict)


def main():
    # Training settings
    parser = argparse.ArgumentParser(description='GNN baselines on ogbg-code data with Pytorch Geometrics')
    parser.add_argument('--device', type=int, default=0,
                        help='which gpu to use if any (default: 0)')
    parser.add_argument('--gnn', type=str, default="mostperfect",  #M_DAGNN_GRU,
                        help='GNN gin, gin-virtual, or gcn, or gcn-virtual (default: gcn-virtual)')
    parser.add_argument('--drop_ratio', type=float, default=0,
                        help='dropout ratio (default: 0)')
    parser.add_argument('--max_seq_len', type=int, default=5,
                        help='maximum sequence length to predict (default: 5)')
    parser.add_argument('--num_vocab', type=int, default=5000,
                        help='the number of vocabulary used for sequence prediction (default: 5000)')
    parser.add_argument('--num_layer', type=int, default=5,
                        help='number of GNN message passing layers (default: 5)')
    parser.add_argument('--emb_dim', type=int, default=300,
                        help='dimensionality of hidden units in GNNs (default: 300)')
    parser.add_argument('--batch_size', type=int, default=1,
                        help='input batch size for training (default: 128)')
    parser.add_argument('--epochs', type=int, default=30,
                        help='number of epochs to train (default: 30)')
    parser.add_argument('--num_workers', type=int, default=0,
                        help='number of workers (default: 0)')
    parser.add_argument('--dataset', type=str, default="ogbg-code",
                        help='dataset name (default: ogbg-code)')

    parser.add_argument('--filename', type=str, default="test",
                        help='filename to output result (default: )')

    parser.add_argument('--dir_data', type=str, default=None,
                        help='... dir')
    parser.add_argument('--dir_results', type=str, default=DIR_RESULTS,
                        help='results dir')
    parser.add_argument('--dir_save', default=DIR_SAVED_MODELS,
                        help='directory to save checkpoints in')
    parser.add_argument('--train_idx', default="",
                        help='...')
    parser.add_argument('--checkpointing', default=1, type=int, choices=[0, 1],
                        help='...')
    parser.add_argument('--checkpoint', default="",
                        help='...')
    parser.add_argument('--folds', default=10, type=int,
                        help='...')
    parser.add_argument('--clip', default=0, type=float,
                        help='...')
    parser.add_argument('--lr', default=1e-3, type=float,
                        help='learning rate (default: 1e-3)')
    parser.add_argument('--patience', default=20, type=float,
                        help='learning rate (default: 1e-3)')
    ###

    args = parser.parse_args()
    args.folds = 1
    args.epochs = 1
    args.checkpointing = 0 # doesn't make sense and yields error for optimizer with current code
    device = torch.device("cuda:" + str(args.device)) if torch.cuda.is_available() else torch.device("cpu")

    os.makedirs(args.dir_results, exist_ok=True)
    os.makedirs(args.dir_save, exist_ok=True)

    train_file = os.path.join(args.dir_results, args.filename + '_train.csv')
    if not os.path.exists(train_file):
        with open(train_file, 'w') as f:
            f.write("fold,epoch,loss,train,valid,test\n")
    res_file = os.path.join(args.dir_results, args.filename + '.csv')
    if not os.path.exists(res_file):
        with open(res_file, 'w') as f:
            f.write("fold,epoch,bestv_train,bestv_valid,bestv_test\n")

    ### automatic dataloading and splitting
    dataset = PygGraphPropPredDataset(name=args.dataset, root="dataset" if args.dir_data is None else args.dir_data)

    seq_len_list = np.array([len(seq) for seq in dataset.data.y])
    print('Target seqence less or equal to {} is {}%.'.format(args.max_seq_len, np.sum(seq_len_list <= args.max_seq_len) / len(seq_len_list)))

    split_idx = dataset.get_idx_split()
    if args.train_idx:
        train_idx = pd.read_csv(os.path.join("dataset", args.train_idx + ".csv.gz"), compression="gzip", header=None).values.T[0]
        train_idx = torch.tensor(train_idx, dtype = torch.long)
        split_idx['train'] = train_idx

    ### building vocabulary for sequence predition. Only use training data.

    vocab2idx, idx2vocab = get_vocab_mapping([dataset.data.y[i] for i in split_idx['train']], args.num_vocab)

    ### set the transform function
    # DAGNN
    augment = augment_edge2 if "dagnn" in args.gnn else augment_edge
    dataset.transform = transforms.Compose([augment, lambda data: encode_y_to_arr(data, vocab2idx, args.max_seq_len)])

    ### automatic evaluator. takes dataset name as input
    evaluator = Evaluator(args.dataset)

    nodeattributes_mapping = pd.read_csv(os.path.join(dataset.root, 'mapping', 'attridx2attr.csv.gz'))

    start_fold = 1
    checkpoint_fn = ""
    train_results, valid_results, test_results = [], [], []     # on fold level

    if args.checkpointing and args.checkpoint:
        s = args.checkpoint[:-3].split("_")
        start_fold = int(s[-2])
        start_epoch = int(s[-1]) + 1

        checkpoint_fn = os.path.join(args.dir_save, args.checkpoint)  # need to remove it in any case

        if start_epoch > args.epochs:  # DISCARD checkpoint's model (ie not results), need a new model!
            args.checkpoint = ""
            start_fold += 1

            results = load_checkpoint_results(checkpoint_fn)
            train_results, valid_results, test_results, train_curve, valid_curve, test_curve = results

    # start
    for fold in range(start_fold, args.folds + 1):
        # fold-specific settings & data splits
        torch.manual_seed(fold)
        random.seed(fold)
        np.random.seed(fold)
        if torch.cuda.is_available():
            torch.cuda.manual_seed(fold)
            torch.backends.cudnn.benchmark = True
            # torch.backends.cudnn.deterministic = True
            # torch.backends.cudnn.benchmark = False

        n_devices = torch.cuda.device_count() if torch.cuda.device_count() > 0 else 1
        # valid_loader = DataLoader(dataset[split_idx["valid"]], batch_size=args.batch_size, shuffle=False,
        #                           num_workers=args.num_workers, n_devices=n_devices)
        test_loader = DataLoader(dataset[split_idx["test"]], batch_size=args.batch_size, shuffle=False,
                                 num_workers=args.num_workers, n_devices=n_devices)

        start_epoch = 1

        # model etc.
        model = init_model(args, vocab2idx, nodeattributes_mapping, idx2vocab)

        print("Let's use", torch.cuda.device_count(), "GPUs! -- DataParallel running also on CPU only")
        device_ids = list(range(torch.cuda.device_count())) if torch.cuda.device_count() > 0 else None
        model = DataParallel(model, device_ids)
        model.to(device)

        optimizer = None #optim.Adam(model.parameters(), lr=args.lr)

        # overwrite some settings
        if args.checkpointing and args.checkpoint:
            # signal that it has been used
            args.checkpoint = ""

            results, start_epoch, model, optimizer = load_checkpoint(checkpoint_fn, model, optimizer)
            train_results, valid_results, test_results, train_curve, valid_curve, test_curve = results
            start_epoch += 1
        else:
            valid_curve, test_curve, train_curve = [], [], []

        # start new epoch
        for epoch in range(start_epoch, args.epochs + 1):
            old_checkpoint_fn = checkpoint_fn
            checkpoint_fn = '%s.pt' % os.path.join(args.dir_save, args.filename + "_" + str(fold) + "_" + str(epoch))

            print("=====Fold {}, Epoch {}".format(fold, epoch))
            loss, train_perf = 0, {"F1": 0}
            valid_perf = {"F1": 0} #eval(model, device, valid_loader, evaluator, arr_to_seq = lambda arr: decode_arr_to_seq(arr, idx2vocab), vocab2index=vocab2idx)
            test_perf = eval(model, device, test_loader, evaluator, arr_to_seq = lambda arr: decode_arr_to_seq(arr, idx2vocab))

            print({'Train': train_perf, 'Validation': valid_perf, 'Test': test_perf})
            with open(train_file, 'a') as f:
                f.write("{},{},{:.4f},{:.4f},{:.4f},{:.4f}\n".format(fold, epoch, loss, train_perf[dataset.eval_metric], valid_perf[dataset.eval_metric], test_perf[dataset.eval_metric]))

            train_curve.append(train_perf[dataset.eval_metric])
            valid_curve.append(valid_perf[dataset.eval_metric])
            test_curve.append(test_perf[dataset.eval_metric])

            ### DAGNN
            if args.checkpointing:
                create_checkpoint(checkpoint_fn, epoch, model, optimizer, (train_results, valid_results, test_results, train_curve, valid_curve, test_curve))
                if fold > 1 or epoch > 1:
                    remove_checkpoint(old_checkpoint_fn)

            best_val_epoch = np.argmax(np.array(valid_curve))
            if args.patience > 0 and best_val_epoch + 1 + args.patience < epoch:
                print("Early stopping!")
                break

        print('Finished training for fold {} !'.format(fold)+"*"*20)
        print('Best validation score: {}'.format(valid_curve[best_val_epoch]))
        print('Test score: {}'.format(test_curve[best_val_epoch]))

        with open(res_file, 'a') as f:
            results = [fold, best_val_epoch, train_curve[best_val_epoch], valid_curve[best_val_epoch],test_curve[best_val_epoch]]
            f.writelines(",".join([str(v) for v in results]) + "\n")

        train_results += [train_curve[best_val_epoch]]
        valid_results += [valid_curve[best_val_epoch]]
        test_results += [test_curve[best_val_epoch]]

        results = list(summary_report(train_results)) + list(summary_report(valid_results)) + list(summary_report(test_results))
        # with open(res_file, 'a') as f:
        #     f.writelines(str(fold)+ ",_," + ",".join([str(v) for v in results]) + "\n")
        print(",".join([str(v) for v in results]))

    results = list(summary_report(train_results)) + list(summary_report(valid_results)) + list(summary_report(test_results))
    with open(res_file, 'a') as f:
        f.writelines(str(fold) + ",_," + ",".join([str(v) for v in results]) + "\n")
        # print(",".join([str(v) for v in results]))

    # we might want to add folds
    # if args.checkpointing:
    #     remove_checkpoint(checkpoint_fn)


def init_model(args, vocab2idx, nodeattributes_mapping, idx2vocab):
    if args.gnn == 'node1':
        model = GuessNodeOneToken(vocab2idx, nodeattributes_mapping, max_seq_len=args.max_seq_len)
    elif args.gnn == 'tokct':
        model = GuessTokensByOccurrence(vocab2idx, nodeattributes_mapping, args.max_seq_len, idx2vocab)
    elif args.gnn == 'perfect':
        model = PerfectModel(vocab2idx, nodeattributes_mapping, args.max_seq_len, idx2vocab)
    elif args.gnn == 'mostperfect':
        model = MostPerfectModel(vocab2idx, args.max_seq_len)

    else:
        raise ValueError('Invalid GNN type')

    return model


if __name__ == "__main__":
    main()