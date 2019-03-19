# speaker_id.py
# Mirco Ravanelli 
# Mila - University of Montreal 

# July 2018

# Description: 
# This code performs a speaker_id experiments with SincNet.
 
# How to run it:
# python speaker_id.py --cfg=cfg/SincNet_TIMIT.cfg

import os
#import scipy.io.wavfile
import soundfile as sf
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable

import sys
import numpy as np
from dnn_models import MLP,flip
from dnn_models import SincNet as CNN 
from data_io import ReadList,read_conf,str_to_bool
from tqdm import tqdm
from datetime import datetime
import subprocess

from torch.serialization import default_restore_location
import glob

import librosa
from VAD_segments import VAD_chunk
from sklearn.preprocessing import normalize  

def create_batches_rnd(batch_size,data_folder,wav_lst,N_snt,wlen,lab_dict,fact_amp):
    
 # Initialization of the minibatch (batch_size,[0=>x_t,1=>x_t+N,1=>random_samp])
 sig_batch=np.zeros([batch_size,wlen])
 lab_batch=np.zeros(batch_size)
  
 snt_id_arr=np.random.randint(N_snt, size=batch_size)
 
 rand_amp_arr = np.random.uniform(1.0-fact_amp,1+fact_amp,batch_size)

 for i in range(batch_size):
     
  # select a random sentence from the list 
  #[fs,signal]=scipy.io.wavfile.read(data_folder+wav_lst[snt_id_arr[i]])
  #signal=signal.astype(float)/32768

  [signal, fs] = sf.read(data_folder+wav_lst[snt_id_arr[i]])

  # accesing to a random chunk
  snt_len=signal.shape[0]
  snt_beg=np.random.randint(snt_len-wlen-1) #randint(0, snt_len-2*wlen-1)
  snt_end=snt_beg+wlen
  
  sig_batch[i,:]=signal[snt_beg:snt_end]*rand_amp_arr[i]
  lab_batch[i]=lab_dict[wav_lst[snt_id_arr[i]]]
  
 inp=Variable(torch.from_numpy(sig_batch).float().cuda().contiguous())
 lab=Variable(torch.from_numpy(lab_batch).float().cuda().contiguous())
  
 return inp,lab  

def concat_segs(times, segs):
  #Concatenate continuous voiced segments
  concat_seg = []
  seg_concat = segs[0]
  for i in range(0, len(times)-1):
    if times[i][1] == times[i+1][0]:
      seg_concat = np.concatenate((seg_concat, segs[i+1]))
    else:
      concat_seg.append(seg_concat)
      seg_concat = segs[i+1]
  else:
    concat_seg.append(seg_concat)
  return concat_seg

# Reading cfg file
options=read_conf()

#[data]
tr_lst=options.tr_lst
te_lst=options.te_lst
pt_file=options.pt_file
class_dict_file=options.lab_dict
data_folder=options.data_folder+'/'
output_folder=options.output_folder

#[windowing]
fs=int(options.fs)
cw_len=int(options.cw_len)
cw_shift=int(options.cw_shift)

#[cnn]
cnn_N_filt=list(map(int, options.cnn_N_filt.split(',')))
cnn_len_filt=list(map(int, options.cnn_len_filt.split(',')))
cnn_max_pool_len=list(map(int, options.cnn_max_pool_len.split(',')))
cnn_use_laynorm_inp=str_to_bool(options.cnn_use_laynorm_inp)
cnn_use_batchnorm_inp=str_to_bool(options.cnn_use_batchnorm_inp)
cnn_use_laynorm=list(map(str_to_bool, options.cnn_use_laynorm.split(',')))
cnn_use_batchnorm=list(map(str_to_bool, options.cnn_use_batchnorm.split(',')))
cnn_act=list(map(str, options.cnn_act.split(',')))
cnn_drop=list(map(float, options.cnn_drop.split(',')))


#[dnn]
fc_lay=list(map(int, options.fc_lay.split(',')))
fc_drop=list(map(float, options.fc_drop.split(',')))
fc_use_laynorm_inp=str_to_bool(options.fc_use_laynorm_inp)
fc_use_batchnorm_inp=str_to_bool(options.fc_use_batchnorm_inp)
fc_use_batchnorm=list(map(str_to_bool, options.fc_use_batchnorm.split(',')))
fc_use_laynorm=list(map(str_to_bool, options.fc_use_laynorm.split(',')))
fc_act=list(map(str, options.fc_act.split(',')))

#[class]
class_lay=list(map(int, options.class_lay.split(',')))
class_drop=list(map(float, options.class_drop.split(',')))
class_use_laynorm_inp=str_to_bool(options.class_use_laynorm_inp)
class_use_batchnorm_inp=str_to_bool(options.class_use_batchnorm_inp)
class_use_batchnorm=list(map(str_to_bool, options.class_use_batchnorm.split(',')))
class_use_laynorm=list(map(str_to_bool, options.class_use_laynorm.split(',')))
class_act=list(map(str, options.class_act.split(',')))


#[optimization]
lr=float(options.lr)
batch_size=int(options.batch_size)
N_epochs=int(options.N_epochs)
N_batches=int(options.N_batches)
N_eval_epoch=int(options.N_eval_epoch)
seed=int(options.seed)


# training list
wav_lst_tr=ReadList(tr_lst)
snt_tr=len(wav_lst_tr)

# test list
wav_lst_te=ReadList(te_lst)
snt_te=len(wav_lst_te)


# Folder creation
try:
    os.stat(output_folder)
except:
    os.mkdir(output_folder) 
    
    
# setting seed
torch.manual_seed(seed)
np.random.seed(seed)

# loss function
cost = nn.NLLLoss()

  
# Converting context and shift in samples
wlen=int(fs*cw_len/1000.00)
wshift=int(fs*cw_shift/1000.00)

# Batch_dev
Batch_dev=128


# Feature extractor CNN
CNN_arch = {'input_dim': wlen,
          'fs': fs,
          'cnn_N_filt': cnn_N_filt,
          'cnn_len_filt': cnn_len_filt,
          'cnn_max_pool_len':cnn_max_pool_len,
          'cnn_use_laynorm_inp': cnn_use_laynorm_inp,
          'cnn_use_batchnorm_inp': cnn_use_batchnorm_inp,
          'cnn_use_laynorm':cnn_use_laynorm,
          'cnn_use_batchnorm':cnn_use_batchnorm,
          'cnn_act': cnn_act,
          'cnn_drop':cnn_drop,          
          }

CNN_net=CNN(CNN_arch)
CNN_net.cuda()

# Loading label dictionary
lab_dict=np.load(class_dict_file).item()



DNN1_arch = {'input_dim': CNN_net.out_dim,
          'fc_lay': fc_lay,
          'fc_drop': fc_drop, 
          'fc_use_batchnorm': fc_use_batchnorm,
          'fc_use_laynorm': fc_use_laynorm,
          'fc_use_laynorm_inp': fc_use_laynorm_inp,
          'fc_use_batchnorm_inp':fc_use_batchnorm_inp,
          'fc_act': fc_act,
          }

DNN1_net=MLP(DNN1_arch)
DNN1_net.cuda()


DNN2_arch = {'input_dim':fc_lay[-1] ,
          'fc_lay': class_lay,
          'fc_drop': class_drop, 
          'fc_use_batchnorm': class_use_batchnorm,
          'fc_use_laynorm': class_use_laynorm,
          'fc_use_laynorm_inp': class_use_laynorm_inp,
          'fc_use_batchnorm_inp':class_use_batchnorm_inp,
          'fc_act': class_act,
          }


DNN2_net=MLP(DNN2_arch)
DNN2_net.cuda()
best_validate = float('inf')
last_epoch = -1
try:
  subprocess.call(['gsutil', 'cp', 'gs://edinquake/asr/SincNet_TIMIT/model_best.pkl', 'model_best.pkl'], stdout=FNULL, stderr=subprocess.STDOUT)
  checkpoint_load = torch.load('model_best.pkl', map_location=lambda s, l: default_restore_location(s, 'cpu'))
  CNN_net.load_state_dict(checkpoint_load['CNN_model_par'])
  DNN1_net.load_state_dict(checkpoint_load['DNN1_model_par'])
  DNN2_net.load_state_dict(checkpoint_load['DNN2_model_par'])
  last_epoch = checkpoint_load['epoch']
  best_validate = checkpoint_load['best_validate']
  print('Checkpoint restored.')
  print('Num_epochs: {}'.format(last_epoch))
  print('Best validation loss: {}'.format(best_validate))

except:
  pass

if pt_file!='none':
  #dataset path
  unprocessed_data = '/Users/Jimmy/baseline/wtf_timit/*/*/*/*.wav'
  audio_path = glob.glob(os.path.dirname(unprocessed_data))  

  total_speaker_num = len(audio_path)
  train_speaker_num= (total_speaker_num//10)*9            # split total data 90% train and 10% test
  
  train_sequence = []
  train_cluster_id = []
  label = 0
  count = 0 
  train_saved = False
  for i, folder in enumerate(audio_path):
    for file in os.listdir(folder):
      if file[-4:] == '.wav':
        times, segs = VAD_chunk(2, folder+'/'+file)
        if segs == []:
          print('No voice activity detected')
          continue
        concat_seg = concat_segs(times, segs)
        for seg in concat_seg:
          # Each segment has one d vector
          train_cluster_id.append(str(label))
          # To calculate the segment embedding
          segment_embeddings = []
          
          signal = torch.from_numpy(seg).float().cuda().contiguous()
          # split signals into chunks
          wlen = int(fs * 250 / 1000.00)
          wshift = int(fs * 125 / 1000.00)

          beg_samp = 0
          end_samp = wlen

          N_fr = int((signal.shape[0]-wlen)/(wshift))

          sig_arr = torch.zeros([Batch_dev, wlen]).float().cuda().contiguous()
          count_fr = 0
          count_fr_tot = 0
          while end_samp < signal.shape[0]:
            sig_arr[count_fr,:] = signal[beg_samp:end_samp]
            beg_samp = beg_samp + wshift
            end_samp = beg_samp + wlen
            count_fr += 1
            count_fr_tot += 1
            if count_fr == Batch_dev:
              inp = Variable(sig_arr)
              embeddings = DNN1_net(CNN_net(inp))
              segment_embeddings.append(embeddings)
              
              count_fr = 0
              sig_arr = torch.zeros([Batch_dev,wlen]).float().cuda().contiguous()

          if count_fr > 0:
            inp = Variable(sig_arr[0:count_fr])
            embeddings = DNN1_net(CNN_net(inp))
            segment_embeddings.append(embeddings)

          # Produce the segment d vector, apply L2 norm then average
          segment_embeddings_norm2 = normalize(segment_embeddings)
          segment_embedding = np.average(segment_embeddings_norm2, axis=0)
          train_sequence.append(segment_embedding)

        count = count + 1
        if count % 100 == 0:
          print('Processed {0}/{1} files'.format(count, len(audio_path)*10))
    
    label = label + 1
    
    if not train_saved and i > train_speaker_num:
      train_sequence = np.concatenate(train_sequence, axis=0)
      train_cluster_id = np.asarray(train_cluster_id)
      np.save('train_sequence',train_sequence)
      np.save('train_cluster_id',train_cluster_id)
      train_saved = True
      train_sequence = []
      train_cluster_id = []
          
  train_sequence = np.concatenate(train_sequence, axis=0)
  train_cluster_id = np.asarray(train_cluster_id)
  np.save('test_sequence',train_sequence)
  np.save('test_cluster_id',train_cluster_id)
  quit()



optimizer_CNN = optim.RMSprop(CNN_net.parameters(), lr=lr,alpha=0.95, eps=1e-8) 
optimizer_DNN1 = optim.RMSprop(DNN1_net.parameters(), lr=lr,alpha=0.95, eps=1e-8) 
optimizer_DNN2 = optim.RMSprop(DNN2_net.parameters(), lr=lr,alpha=0.95, eps=1e-8) 


print('***Started training at {}***'.format(datetime.now()))
for epoch in range(last_epoch + 1, N_epochs):
  
  test_flag=0
  CNN_net.train()
  DNN1_net.train()
  DNN2_net.train()
 
  loss_sum=0
  err_sum=0

  for i in tqdm(range(N_batches)):

    [inp,lab]=create_batches_rnd(batch_size,data_folder,wav_lst_tr,snt_tr,wlen,lab_dict,0.2)
    pout=DNN2_net(DNN1_net(CNN_net(inp)))
    
    pred=torch.max(pout,dim=1)[1]
    loss = cost(pout, lab.long())
    err = torch.mean((pred!=lab.long()).float())
    
   
    
    optimizer_CNN.zero_grad()
    optimizer_DNN1.zero_grad() 
    optimizer_DNN2.zero_grad() 
    
    loss.backward()
    optimizer_CNN.step()
    optimizer_DNN1.step()
    optimizer_DNN2.step()
    
    loss_sum=loss_sum+loss.detach()
    err_sum=err_sum+err.detach()
 

  loss_tot=loss_sum/N_batches
  err_tot=err_sum/N_batches
    
   
  # Full Validation  new        
  CNN_net.eval()
  DNN1_net.eval()
  DNN2_net.eval()
  test_flag=1 
  loss_sum=0
  err_sum=0
  err_sum_snt=0
 
  with torch.no_grad():  
    for i in range(snt_te):
     
      #[fs,signal]=scipy.io.wavfile.read(data_folder+wav_lst_te[i])
      #signal=signal.astype(float)/32768

      [signal, fs] = sf.read(data_folder+wav_lst_te[i])

      signal=torch.from_numpy(signal).float().cuda().contiguous()
      lab_batch=lab_dict[wav_lst_te[i]]
  
      # split signals into chunks
      beg_samp=0
      end_samp=wlen
   
      N_fr=int((signal.shape[0]-wlen)/(wshift))
   

      sig_arr=torch.zeros([Batch_dev,wlen]).float().cuda().contiguous()
      lab= Variable((torch.zeros(N_fr+1)+lab_batch).cuda().contiguous().long())
      pout=Variable(torch.zeros(N_fr+1,class_lay[-1]).float().cuda().contiguous())
      count_fr=0
      count_fr_tot=0
      while end_samp<signal.shape[0]:
        sig_arr[count_fr,:]=signal[beg_samp:end_samp]
        beg_samp=beg_samp+wshift
        end_samp=beg_samp+wlen
        count_fr=count_fr+1
        count_fr_tot=count_fr_tot+1
        if count_fr==Batch_dev:
          inp=Variable(sig_arr)
          pout[count_fr_tot-Batch_dev:count_fr_tot,:]=DNN2_net(DNN1_net(CNN_net(inp)))
          count_fr=0
          sig_arr=torch.zeros([Batch_dev,wlen]).float().cuda().contiguous()
 
      if count_fr>0:
        inp=Variable(sig_arr[0:count_fr])
        pout[count_fr_tot-count_fr:count_fr_tot,:]=DNN2_net(DNN1_net(CNN_net(inp)))

      pred=torch.max(pout,dim=1)[1]
      loss = cost(pout, lab.long())
      err = torch.mean((pred!=lab.long()).float())
  
      [val,best_class]=torch.max(torch.sum(pout,dim=0),0)
      err_sum_snt=err_sum_snt+(best_class!=lab[0]).float()
  
      loss_sum=loss_sum+loss.detach()
      err_sum=err_sum+err.detach()
      
    err_tot_dev_snt=err_sum_snt/snt_te
    loss_tot_dev=loss_sum/snt_te
    err_tot_dev=err_sum/snt_te

    if err_tot_dev <= best_validate:
      best_validate = err_tot_dev
      checkpoint={'CNN_model_par': CNN_net.state_dict(),
             'DNN1_model_par': DNN1_net.state_dict(),
             'DNN2_model_par': DNN2_net.state_dict(),
             'epoch': epoch,
             'best_validate': best_validate,
             }
      torch.save(checkpoint,output_folder+'/model_best.pkl')
      subprocess.call(['gsutil', 'cp', output_folder+'/model_best.pkl', 'gs://edinquake/asr/SincNet_TIMIT/model_best.pkl'])



    print("epoch %i, loss_tr=%f err_tr=%f loss_te=%f err_te=%f err_te_snt=%f" % (epoch, loss_tot,err_tot,loss_tot_dev,err_tot_dev,err_tot_dev_snt))

    with open(output_folder+"/res.res", "a") as res_file:
      res_file.write("epoch %i, loss_tr=%f err_tr=%f loss_te=%f err_te=%f err_te_snt=%f\n" % (epoch, loss_tot,err_tot,loss_tot_dev,err_tot_dev,err_tot_dev_snt))   

    checkpoint={'CNN_model_par': CNN_net.state_dict(),
             'DNN1_model_par': DNN1_net.state_dict(),
             'DNN2_model_par': DNN2_net.state_dict(),
             'epoch': epoch,
             'best_validate': best_validate,
             }
    torch.save(checkpoint,output_folder+'/model_last.pkl')