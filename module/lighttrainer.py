import lightning as pl
import torch
import torch.nn.functional as F
import torch.nn as nn
import logging
from module.evaluation import FocalLoss
from module.preprocessing import get_pair_pad_idx, get_pad_idx
from transformers import get_cosine_schedule_with_warmup
from module.evaluation import FocalLoss #log_metrics, 
from sklearn.metrics import classification_report, precision_score , recall_score , confusion_matrix
from transformers import AutoModel
import numpy as np
from module.lightmodels import TotalModel, OriginalPRG_MoE, TotalModel_cause_fc

class LitPRGMoE(pl.LightningModule):
    def __init__(self, **kwargs):
        super().__init__()
        
        # 모델 셋팅 파라미터
        self.encoder_name = kwargs['encoder_name']
        # Model
        
        self.use_original = kwargs['use_original']
        self.use_newfc = kwargs['use_newfc']
        
        if self.use_original:
            self.model = OriginalPRG_MoE() # output: (emotion prediction, cause prediction)
        else:
            if self.use_newfc:
                self.model = TotalModel_cause_fc(self.encoder_name) # output: (emotion prediction, cause prediction)
            else:
                self.model = TotalModel(self.encoder_name) # output: (emotion prediction, cause prediction)

        # 하이퍼파라미터 설정
        self.training_iter = kwargs['training_iter']
        self.dropout = kwargs['dropout']
        self.learning_rate = kwargs['learning_rate']
        self.window_size = 3
        self.n_expert = 4
        self.n_emotion = 7
        self.guiding_lambda = kwargs['guiding_lambda']
        self.loss_lambda = kwargs['loss_lambda'] # loss 중 Emotion loss의 비율
        
        # 학습 방법 설정
        self.num_unfreeze = kwargs['unfreeze']
        self.only_emotion = False
        self.n_cause = kwargs['n_cause']
        self.emotion_epoch_ratio = kwargs['emotion_epoch_ratio'] # 이 비율만큼 추가적으로 먼저 감정만 학습
        self.ckpt_type = kwargs['ckpt_type']
        
        # 모델 내 학습 중 변수 설정
        self.test = False # True when testing(on_test_epoch_start ~ on_test_epoch_end)
            # test에서 joint_accuracy를 계산
        self.train_type = 'total'
        
        if 'bert-base' in self.encoder_name:
            self.is_bert_like = True
        else:
            self.is_bert_like = False
                        
        self.emotion_epoch = int(self.training_iter * self.emotion_epoch_ratio)
        self.training_iter = self.training_iter + self.emotion_epoch
        
        # Dictionaries for logging
        types = ['train', 'valid', 'test']
        self.emo_pred_y_list = {}
        self.emo_true_y_list = {}
        self.cau_pred_y_list = {}
        self.cau_true_y_list = {}
        self.cau_pred_y_list_all = {}
        self.cau_true_y_list_all = {}
        self.emo_cause_pred_y_list = {}
        self.emo_cause_true_y_list = {}
        self.emo_cause_pred_y_list_all = {}
        self.emo_cause_true_y_list_all = {}
        self.loss_sum = {}
        self.batch_count = {}
        
        for i in types:
            self.emo_pred_y_list[i] = []
            self.emo_true_y_list[i] = []
            self.cau_pred_y_list[i] = []
            self.cau_true_y_list[i] = []
            self.cau_pred_y_list_all[i] = []
            self.cau_true_y_list_all[i] = []
            self.emo_cause_pred_y_list[i] = []
            self.emo_cause_true_y_list[i] = []
            self.emo_cause_pred_y_list_all[i] = []
            self.emo_cause_true_y_list_all[i] = []
            self.loss_sum[i] = 0.0
            self.batch_count[i] = 0
        
        self.best_performance_emo = {
            'accuracy': 0.0,
            'macro_f1': 0.0,
            'weighted_f1': 0.0,
            'epoch': 0,
            'loss': 0.0,
        }
        self.best_performance_cau = {
            'accuracy': 0.0,
            'precision': 0.0,
            'recall': 0.0,
            'f1': 0.0,
            'epoch': 0,
            'loss': 0.0,
        }
        self.best_performance_emo_cau = {
            'epoch': 0,
            'precision': 0.0,
            'recall': 0.0,
            'f1': 0.0,
        }
        
    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(self.parameters(), lr=self.learning_rate)
        scheduler = get_cosine_schedule_with_warmup(optimizer=optimizer,
                                                    num_warmup_steps=5,
                                                    num_training_steps=self.training_iter,
                                                    )
        return [optimizer], [scheduler]
                    
    def forward(self, batch):
        utterance_input_ids_batch, utterance_attention_mask_batch, utterance_token_type_ids_batch, speaker_batch, emotion_label_batch, pair_cause_label_batch, pair_binary_cause_label_batch = batch
        
        batch_size, max_doc_len, max_seq_len = utterance_input_ids_batch.shape
        
        input_ids = utterance_input_ids_batch
        attention_mask = utterance_attention_mask_batch
        token_type_ids = utterance_token_type_ids_batch
        speaker_ids = speaker_batch
        
        # Forward
        emotion_prediction, cause_prediction = self.model(input_ids, attention_mask, token_type_ids, speaker_ids, max_seq_len)
            # batch_size, max_doc_len, max_seq_len = [5, 28, 75] 인 경우,
            # emotion_prediction, cause_prediction =  [140, 7], [2030, 2](5*(28*29/2)=5*406)
                # batch(dialog) 당 406개의 pair가 생성됨( 28개의 utterance들을 각각 그 이전 발화 전부들과 pairing )
            
        
        # if self.test:
        #     # emotion 예측값을 기준으로, 각 대화 속에 있는 Non-neutral인 발화에 대해 w개의 발화를 후보로 선정
        #     check_pair_window_idx = get_pair_pad_idx(utterance_input_ids_batch, self.encoder_name, window_constraint=self.window_size, emotion_pred=emotion_prediction)
            
        #     # Emotion 예측과 관계없이, 발화의 쌍이 될 수 있는 모든 경우에 대해 index를 1로 설정한 check_idx
        #     check_pair_pad_idx = get_pair_pad_idx(utterance_input_ids_batch, self.encoder_name, window_constraint=1000, )
        #     self.joint_accuracy_step(emotion_prediction, emotion_label_batch, cause_prediction, pair_binary_cause_label_batch, check_pair_pad_idx, check_pair_window_idx, batch_size, self.n_cause)
        
        return (emotion_prediction, cause_prediction)
    
    def output_processing(self, utterance_input_ids_batch, pair_binary_cause_label_batch, emotion_label_batch, emotion_prediction, binary_cause_prediction):
        # 모델의 forward 결과로부터 loss 계산과 로깅을 위한 input 6개를 구해 리턴
        batch_size, _, _ = utterance_input_ids_batch.shape
        # Output processing
        check_pair_window_idx = get_pair_pad_idx(utterance_input_ids_batch, self.encoder_name, window_constraint=self.window_size, emotion_pred=emotion_prediction)
        check_pair_pad_idx = get_pair_pad_idx(utterance_input_ids_batch, self.encoder_name, window_constraint=1000, )
        check_pad_idx = get_pad_idx(utterance_input_ids_batch, self.encoder_name)

        # emotion pred/label을 pair candidate만큼 늘림
        emotion_list = emotion_prediction.view(batch_size, -1, 7)
        emotion_pair_list = []
        emotion_pred_list = []
        for doc_emotion in emotion_list: # 전체 batch에서 각 doc(대화)을 가져옴
                end_t = 0
                for utt_emotion in doc_emotion: # 각 대화마다 utterance 가져옴
                    emotion_pred_list.append(torch.argmax(utt_emotion))
                    for _ in range(end_t+1): # 
                        emotion_pair_list.append(torch.argmax(utt_emotion)) # 모델의 감정 예측을 index[7->1]화
                    end_t += 1
        binary_cause_pred_window_full = torch.argmax(binary_cause_prediction.view(batch_size, -1, self.n_cause), dim=-1)
        emotion_label_pair_list = [] 
        for doc_emotion in emotion_label_batch:
            end_t = 0
            for emotion in doc_emotion:
                for _ in range(end_t+1):
                    emotion_label_pair_list.append(emotion)
                end_t += 1
        emotion_pair_pred_expanded = torch.stack(emotion_pair_list).view(batch_size, -1)
        emotion_pair_true_expanded = torch.stack(emotion_label_pair_list).view(batch_size, -1)
        
        
        # Emotion prediction, label
        emotion_prediction_filtered = emotion_prediction[(check_pad_idx != False).nonzero(as_tuple=True)]
        emotion_label_batch_filtered = emotion_label_batch.view(-1)[(check_pad_idx != False).nonzero(as_tuple=True)]
        
        # Cause prediction, label
        pair_binary_cause_prediction_window = binary_cause_prediction.view(batch_size, -1, self.n_cause)[(check_pair_window_idx != False).nonzero(as_tuple=True)]
        pair_binary_cause_prediction_all = binary_cause_prediction.view(batch_size, -1, self.n_cause)[(check_pair_pad_idx != False).nonzero(as_tuple=True)]
        
        pair_binary_cause_label_batch_window = pair_binary_cause_label_batch[(check_pair_window_idx != False).nonzero(as_tuple=True)]
        pair_binary_cause_label_batch_all = pair_binary_cause_label_batch[(check_pair_pad_idx != False).nonzero(as_tuple=True)]
        
        # Emotion-cause prediction, label
        pair_emotion_prediction_window = emotion_pair_pred_expanded[(check_pair_window_idx != False).nonzero(as_tuple=True)]
        pair_emotion_prediction_all = emotion_pair_pred_expanded[(check_pair_pad_idx != False).nonzero(as_tuple=True)]
        pair_emotion_label_batch_window = emotion_pair_true_expanded[(check_pair_window_idx != False).nonzero(as_tuple=True)]
        pair_emotion_label_batch_all = emotion_pair_true_expanded[(check_pair_pad_idx != False).nonzero(as_tuple=True)]
        
        # Emotion label에 10을 곱함 (0~6 -> 0~60). Cause 여부는 0,1만 있기 때문에 10을 넘길 수 없어서 이래야 충돌 없음
        pair_emotion_cause_prediction_window = pair_emotion_prediction_window*10 + torch.argmax(pair_binary_cause_prediction_window, dim=1)
        pair_emotion_cause_prediction_all = pair_emotion_prediction_all*10 + torch.argmax(pair_binary_cause_prediction_all, dim=1)
        pair_emotion_cause_label_batch_window = pair_emotion_label_batch_window*10 + pair_binary_cause_label_batch_window
        pair_emotion_cause_label_batch_all = pair_emotion_label_batch_all*10 + pair_binary_cause_label_batch_all
        
        emotion_ = (emotion_prediction_filtered, emotion_label_batch_filtered)
        cause_ = (pair_binary_cause_prediction_window, pair_binary_cause_prediction_all, pair_binary_cause_label_batch_window, pair_binary_cause_label_batch_all)
        emotion_cause_ = (pair_emotion_cause_prediction_window, pair_emotion_cause_prediction_all, pair_emotion_cause_label_batch_window, pair_emotion_cause_label_batch_all)
        return (emotion_, cause_, emotion_cause_)
    
    
    
    def loss_calculation(self, emotion_prediction_filtered, emotion_label_batch_filtered, pair_binary_cause_prediction_window, pair_binary_cause_label_batch_window):
        if self.train_type == 'cause':
            # criterion_emo = FocalLoss(gamma=2)
            criterion_cau = FocalLoss(gamma=2)
            
            # loss_emo = criterion_emo(emotion_prediction_filtered, emotion_label_batch_filtered)
            loss_cau = criterion_cau(pair_binary_cause_prediction_window, pair_binary_cause_label_batch_window)
            # loss = 0.2 * loss_emo + 0.8 * loss_cau
            loss = loss_cau
        elif self.train_type == 'emotion':
            criterion_emo = FocalLoss(gamma=2)
            loss_emo = criterion_emo(emotion_prediction_filtered, emotion_label_batch_filtered)
            loss = loss_emo
        elif self.train_type == 'total':
            criterion_emo = FocalLoss(gamma=2)
            criterion_cau = FocalLoss(gamma=2)
            
            loss_emo = criterion_emo(emotion_prediction_filtered, emotion_label_batch_filtered)
            loss_cau = criterion_cau(pair_binary_cause_prediction_window, pair_binary_cause_label_batch_window)
            loss = self.loss_lambda * loss_emo + (1-self.loss_lambda) * loss_cau
        return loss
    
    
    def training_step(self, batch, batch_idx):
        types = 'train'
        # utterance_input_ids_batch, utterance_attention_mask_batch, utterance_token_type_ids_batch, speaker_batch, emotion_label_batch, pair_cause_label_batch, pair_binary_cause_label_batch = batch
        utterance_input_ids_batch, _, _, _, emotion_label_batch, _, pair_binary_cause_label_batch = batch
        
        emotion_prediction, binary_cause_prediction = self.forward(batch)
        
        # Output processing
        (emotion_, cause_, emotion_cause_) = self.output_processing(utterance_input_ids_batch, pair_binary_cause_label_batch, emotion_label_batch, emotion_prediction, binary_cause_prediction)
        
        (emotion_prediction_filtered, emotion_label_batch_filtered) = emotion_
        (pair_binary_cause_prediction_window, pair_binary_cause_prediction_all, pair_binary_cause_label_batch_window, pair_binary_cause_label_batch_all) = cause_
        (pair_emotion_cause_prediction_window, pair_emotion_cause_prediction_all, pair_emotion_cause_label_batch_window, pair_emotion_cause_label_batch_all) = emotion_cause_
        
        # Logging
        self.cau_pred_y_list_all[types].append(pair_binary_cause_prediction_all), self.cau_true_y_list_all[types].append(pair_binary_cause_label_batch_all)
        self.cau_pred_y_list[types].append(pair_binary_cause_prediction_window), self.cau_true_y_list[types].append(pair_binary_cause_label_batch_window)
        self.emo_pred_y_list[types].append(emotion_prediction_filtered), self.emo_true_y_list[types].append(emotion_label_batch_filtered)
        self.emo_cause_pred_y_list[types].append(pair_emotion_cause_prediction_all), self.emo_cause_true_y_list[types].append(pair_emotion_cause_label_batch_all)
        self.emo_cause_pred_y_list_all[types].append(pair_emotion_cause_prediction_window), self.emo_cause_true_y_list_all[types].append(pair_emotion_cause_label_batch_window)
        
        # Loss Calculation
        loss = self.loss_calculation(emotion_prediction_filtered, emotion_label_batch_filtered, pair_binary_cause_prediction_window, pair_binary_cause_label_batch_window)
        
        self.loss_sum[types] += loss.item()
        self.batch_count[types] += 1
        
        return loss
    
    def validation_step(self, batch, batch_idx):
        types = 'valid'
        utterance_input_ids_batch, _, _, _, emotion_label_batch, _, pair_binary_cause_label_batch = batch
        
        emotion_prediction, binary_cause_prediction = self.forward(batch)
        
        # Output processing
        (emotion_, cause_, emotion_cause_) = self.output_processing(utterance_input_ids_batch, pair_binary_cause_label_batch, emotion_label_batch, emotion_prediction, binary_cause_prediction)
        
        (emotion_prediction_filtered, emotion_label_batch_filtered) = emotion_
        (pair_binary_cause_prediction_window, pair_binary_cause_prediction_all, pair_binary_cause_label_batch_window, pair_binary_cause_label_batch_all) = cause_
        (pair_emotion_cause_prediction_window, pair_emotion_cause_prediction_all, pair_emotion_cause_label_batch_window, pair_emotion_cause_label_batch_all) = emotion_cause_
        
        # Loss Calculation
        loss = self.loss_calculation(emotion_prediction_filtered, emotion_label_batch_filtered, pair_binary_cause_prediction_window, pair_binary_cause_label_batch_window)
            
        self.log("valid_loss: ", loss, sync_dist=True)
        # Logging
        self.cau_pred_y_list_all[types].append(pair_binary_cause_prediction_all), self.cau_true_y_list_all[types].append(pair_binary_cause_label_batch_all)
        self.cau_pred_y_list[types].append(pair_binary_cause_prediction_window), self.cau_true_y_list[types].append(pair_binary_cause_label_batch_window)
        self.emo_pred_y_list[types].append(emotion_prediction_filtered), self.emo_true_y_list[types].append(emotion_label_batch_filtered)
        self.emo_cause_pred_y_list[types].append(pair_emotion_cause_prediction_all), self.emo_cause_true_y_list[types].append(pair_emotion_cause_label_batch_all)
        self.emo_cause_pred_y_list_all[types].append(pair_emotion_cause_prediction_window), self.emo_cause_true_y_list_all[types].append(pair_emotion_cause_label_batch_window)
        
        self.loss_sum[types] += loss.item()
        self.batch_count[types] += 1
        
    def test_step(self, batch, batch_idx):
        types = 'test'
        utterance_input_ids_batch, utterance_attention_mask_batch, utterance_token_type_ids_batch, speaker_batch, emotion_label_batch, pair_cause_label_batch, pair_binary_cause_label_batch = batch
        
        emotion_prediction, binary_cause_prediction = self.forward(batch)
        
        # Output processing
        (emotion_, cause_, emotion_cause_) = self.output_processing(utterance_input_ids_batch, pair_binary_cause_label_batch, emotion_label_batch, emotion_prediction, binary_cause_prediction)
        
        (emotion_prediction_filtered, emotion_label_batch_filtered) = emotion_
        (pair_binary_cause_prediction_window, pair_binary_cause_prediction_all, pair_binary_cause_label_batch_window, pair_binary_cause_label_batch_all) = cause_
        (pair_emotion_cause_prediction_window, pair_emotion_cause_prediction_all, pair_emotion_cause_label_batch_window, pair_emotion_cause_label_batch_all) = emotion_cause_
        
        # Loss Calculation
        loss = self.loss_calculation(emotion_prediction_filtered, emotion_label_batch_filtered, pair_binary_cause_prediction_window, pair_binary_cause_label_batch_window)
            
        self.log("test_loss: ", loss, sync_dist=True)
        # Logging
        self.cau_pred_y_list_all[types].append(pair_binary_cause_prediction_all), self.cau_true_y_list_all[types].append(pair_binary_cause_label_batch_all)
        self.cau_pred_y_list[types].append(pair_binary_cause_prediction_window), self.cau_true_y_list[types].append(pair_binary_cause_label_batch_window)
        self.emo_pred_y_list[types].append(emotion_prediction_filtered), self.emo_true_y_list[types].append(emotion_label_batch_filtered)
        self.emo_cause_pred_y_list[types].append(pair_emotion_cause_prediction_all), self.emo_cause_true_y_list[types].append(pair_emotion_cause_label_batch_all)
        self.emo_cause_pred_y_list_all[types].append(pair_emotion_cause_prediction_window), self.emo_cause_true_y_list_all[types].append(pair_emotion_cause_label_batch_window)
        
        self.loss_sum[types] += loss.item()
        self.batch_count[types] += 1

    def on_train_epoch_start(self):
        self.make_test_setting(types='train')
        print('Train type: ', self.train_type)
        
    def on_train_epoch_end(self):
        self.log_test_result(types='train')
    
    def on_validation_epoch_start(self):
        self.test = True
        self.make_test_setting(types='valid')

    def on_validation_epoch_end(self):
        self.test = False
        self.log_test_result(types='valid')
    
    def on_test_epoch_start(self):
        self.test = True
        self.make_test_setting(types='test')
        
    def on_test_epoch_end(self):
        self.test = False
        self.log_test_result(types='test')
        
    def make_test_setting(self, types='train'):
        self.emo_pred_y_list[types] = []
        self.emo_true_y_list[types] = []
        self.cau_pred_y_list[types] = []
        self.cau_true_y_list[types] = []
        self.cau_pred_y_list_all[types] = []
        self.cau_true_y_list_all[types] = []
        self.emo_cause_pred_y_list[types] = []
        self.emo_cause_true_y_list[types] = []
        self.emo_cause_pred_y_list_all[types] = []
        self.emo_cause_true_y_list_all[types] = []
        self.loss_sum[types] = 0.0
        self.batch_count[types] = 0
        
    def log_test_result(self, types='train'):
        logger = logging.getLogger(types)
        
        loss_avg = self.loss_sum[types] / self.batch_count[types]
        emo_report, emo_metrics, acc_cau, p_cau, r_cau, f1_cau, p_emo_cau, r_emo_cau, f1_emo_cau = log_metrics(self.emo_pred_y_list[types], self.emo_true_y_list[types], 
                                                self.cau_pred_y_list[types], self.cau_true_y_list[types],
                                                self.cau_pred_y_list_all[types], self.cau_true_y_list_all[types], 
                                                self.emo_cause_pred_y_list[types], self.emo_cause_true_y_list[types],
                                                self.emo_cause_pred_y_list_all[types], self.emo_cause_true_y_list_all[types],
                                                loss_avg)
        
        self.log('binary_cause 1.loss', loss_avg, sync_dist=True)
        self.log('binary_cause 2.accuracy', acc_cau, sync_dist=True)
        self.log('binary_cause 3.precision', p_cau, sync_dist=True)
        self.log('binary_cause 4.recall', r_cau, sync_dist=True)
        self.log('binary_cause 5.f1-score', f1_cau, sync_dist=True)
        
        self.log('emo 1.accuracy', emo_metrics[0], sync_dist=True)
        self.log('emo 2.macro-f1', emo_metrics[1], sync_dist=True)
        self.log('emo 3.weighted-f1', emo_metrics[2], sync_dist=True)
        
        self.log('emo-cau 1.precision', p_emo_cau, sync_dist=True)
        self.log('emo-cau 2.recall', r_emo_cau, sync_dist=True)
        self.log('emo-cau 3.f1-score', f1_emo_cau, sync_dist=True)
        
        logging_texts = f'\n[Epoch {self.current_epoch}] / <Emotion Prediction> of {types}\n'+\
                        f'Train type: {self.train_type}\n'+\
                        emo_report+\
                        f'\n<Cause Prediction>'+\
                        f'\n\taccuracy: \t{acc_cau}'+\
                        f'\n\tprecision:\t{p_cau}'+\
                        f'\n\trecall:   \t{r_cau}'+\
                        f'\n\tf1-score: \t{f1_cau}'+\
                        f'\n\n<Emotion-Cause Prediction>'+\
                        f'\n\tprecision:\t{p_emo_cau}'+\
                        f'\n\trecall:   \t{r_emo_cau}'+\
                        f'\n\tf1-score: \t{f1_emo_cau}'+\
                        f'\n'
                        
        if (types == 'valid'):
            if (self.best_performance_emo['weighted_f1'] < emo_metrics[2]):
                self.best_performance_emo['weighted_f1'] = emo_metrics[2]
                self.best_performance_emo['accuracy'] = emo_metrics[0]
                self.best_performance_emo['macro_f1'] = emo_metrics[1]
                self.best_performance_emo['epoch'] = self.current_epoch
                self.best_performance_emo['loss'] = loss_avg
            if (self.best_performance_cau['f1'] < f1_cau):
                self.best_performance_cau['f1'] = f1_cau
                self.best_performance_cau['accuracy'] = acc_cau
                self.best_performance_cau['precision'] = p_cau
                self.best_performance_cau['recall'] = r_cau
                self.best_performance_cau['epoch'] = self.current_epoch
                self.best_performance_cau['loss'] = loss_avg
            if (self.best_performance_emo_cau['f1'] < f1_emo_cau):
                self.best_performance_emo_cau['precision'] = p_emo_cau
                self.best_performance_emo_cau['recall'] = r_emo_cau
                self.best_performance_emo_cau['f1'] = f1_emo_cau
                self.best_performance_emo_cau['epoch'] = self.current_epoch
            
            appended_log_valid = f'\nCurrent Best Performance: loss: {self.best_performance_cau["loss"]}\n'+\
                            f'\t<Emotion Prediction: [Epoch: {self.best_performance_emo["epoch"]}]>\n'+\
                            f'\t\taccuracy: \t{self.best_performance_emo["accuracy"]}\n'+\
                            f'\t\tmacro_f1: \t{self.best_performance_emo["macro_f1"]}\n'+\
                            f'\t\tweighted_f1: \t{self.best_performance_emo["weighted_f1"]}\n'+\
                            f'\t<Cause Prediction: [Epoch: {self.best_performance_cau["epoch"]}]>\n'+\
                            f'\t\taccuracy: \t{self.best_performance_cau["accuracy"]}\n'+\
                            f'\t\tprecision: \t{self.best_performance_cau["precision"]}\n'+\
                            f'\t\trecall: \t{self.best_performance_cau["recall"]}\n'+\
                            f'\t\tf1:\t\t{self.best_performance_cau["f1"]}\n'+\
                            f'\t<Emotion-Cause Prediction: [Epoch: {self.best_performance_emo_cau["epoch"]}]>\n'+\
                            f'\t\tprecision: \t{self.best_performance_emo_cau["precision"]}\n'+\
                            f'\t\trecall: \t{self.best_performance_emo_cau["recall"]}\n'+\
                            f'\t\tf1:\t\t{self.best_performance_emo_cau["f1"]}\n'
            
        if (types == 'valid'):
            logging_texts += appended_log_valid
        logger.info(logging_texts)
        
    def get_pair_embedding(self, pooled_output, emotion_prediction, input_ids, attention_mask, token_type_ids, speaker_ids):
        batch_size, max_doc_len, max_seq_len = input_ids.shape
        
        utterance_representation = self.dropout(pooled_output)

        concatenated_embedding = torch.cat((utterance_representation, emotion_prediction, 
                                            speaker_ids.view(-1).unsqueeze(1)), dim=1)
        
        pair_embedding = list()
        for batch in concatenated_embedding.view(batch_size, max_doc_len, -1):
            pair_per_batch = list()
            for end_t in range(max_doc_len):
                for t in range(end_t + 1):
                    pair_per_batch.append(torch.cat((batch[t], batch[end_t])))
            pair_embedding.append(torch.stack(pair_per_batch))

        pair_embedding = torch.stack(pair_embedding).to(input_ids.device)

        return pair_embedding
    
    def get_subtask_label(self, input_ids, speaker_ids, emotion_prediction):
        batch_size, max_doc_len, max_seq_len = input_ids.shape

        pair_info = []
        for speaker_batch, emotion_batch in zip(speaker_ids.view(batch_size, max_doc_len, -1), emotion_prediction.view(batch_size, max_doc_len, -1)):
            info_pair_per_batch = []
            for end_t in range(max_doc_len):
                for t in range(end_t + 1):
                    speaker_condition = speaker_batch[t] == speaker_batch[end_t]
                    emotion_condition = torch.argmax(
                        emotion_batch[t]) == torch.argmax(emotion_batch[end_t])

                    if speaker_condition and emotion_condition:
                        # if speaker and dominant emotion are same
                        info_pair_per_batch.append(torch.Tensor([1, 0, 0, 0]))
                    elif speaker_condition:
                        # if speaker is same, but dominant emotion is differnt
                        info_pair_per_batch.append(torch.Tensor([0, 1, 0, 0]))
                    elif emotion_condition:
                        # if speaker is differnt, but dominant emotion is same
                        info_pair_per_batch.append(torch.Tensor([0, 0, 1, 0]))
                    else:
                        # if speaker and dominant emotion are differnt
                        info_pair_per_batch.append(torch.Tensor([0, 0, 0, 1]))
            pair_info.append(torch.stack(info_pair_per_batch))

        pair_info = torch.stack(pair_info).to(input_ids.device)

        return pair_info
    
    
def log_metrics(emo_pred_y_list, emo_true_y_list, 
                cau_pred_y_list, cau_true_y_list, cau_pred_y_list_all, cau_true_y_list_all, 
                emo_cause_pred_y_list, emo_cause_true_y_list, emo_cause_pred_y_list_all, emo_cause_true_y_list_all,                
                loss_avg):
    # <<[[ Emotion 부분 ]]>>
    label_ = np.array(['angry', 'disgust', 'fear', 'happy', 'sad', 'surprise', 'neutral'])
    # logger.info('\n' + metrics_report(torch.cat(emo_pred_y_list), torch.cat(emo_true_y_list), label=label_))
    emo_report_dict = metrics_report(torch.cat(emo_pred_y_list), torch.cat(emo_true_y_list), label=label_, get_dict=True)
    emo_report_str = metrics_report(torch.cat(emo_pred_y_list), torch.cat(emo_true_y_list), label=label_, get_dict=False)
    acc_emo, macro_f1, weighted_f1 = emo_report_dict['accuracy'], emo_report_dict['macro avg']['f1-score'], emo_report_dict['weighted avg']['f1-score']
    emo_metrics = (acc_emo, macro_f1, weighted_f1)
    
    # <<[[  Cause 부분  ]]>>
    label_ = np.array(['No Cause', 'Cause'])
    report_dict = metrics_report(torch.cat(cau_pred_y_list), torch.cat(cau_true_y_list), label=label_, get_dict=True)
    if 'Cause' in report_dict.keys():   #추가된 부분
        _, p_cau, _, _ = report_dict['accuracy'], report_dict['Cause']['precision'], report_dict['Cause']['recall'], report_dict['Cause']['f1-score']
    else:   #추가된 부분
        _, p_cau, _, _ = 0, 0, 0, 0   #추가된 부분
        
    report_dict = metrics_report(torch.cat(cau_pred_y_list_all), torch.cat(cau_true_y_list_all), label=label_, get_dict=True)
    if 'Cause' in report_dict.keys():   #추가된 부분
        acc_cau, _, r_cau, _ = report_dict['accuracy'], report_dict['Cause']['precision'], report_dict['Cause']['recall'], report_dict['Cause']['f1-score']
    else:   #추가된 부분
        acc_cau, _, r_cau, _ = 0, 0, 0, 0   #추가된 부분
        
    f1_cau = 2 * p_cau * r_cau / (p_cau + r_cau) if p_cau + r_cau != 0 else 0
    
    # <<[[ Emotion-cause 부분 ]]>>
    # emo_cause_pred_y_list, emo_cause_true_y_list, emo_cause_pred_y_list_all, emo_cause_true_y_list_all,  
    
    # print('emo_cause_list: \n', confusion_matrix(torch.cat(emo_cause_true_y_list).to('cpu'), torch.cat(emo_cause_pred_y_list).to('cpu')), '\n')
    # print('emo_cause_list_all: \n', confusion_matrix(torch.cat(emo_cause_true_y_list_all).to('cpu'), torch.cat(emo_cause_pred_y_list_all).to('cpu')), '\n')
    
    p_emo_cau = precision_score(torch.cat(emo_cause_true_y_list).to('cpu'), torch.cat(emo_cause_pred_y_list).to('cpu'), average= "macro")
    r_emo_cau = recall_score(torch.cat(emo_cause_true_y_list_all).to('cpu'), torch.cat(emo_cause_pred_y_list_all).to('cpu'), average= "macro")
    f1_emo_cau = 2 * p_emo_cau * r_emo_cau / (p_emo_cau + r_emo_cau) if p_emo_cau + r_emo_cau != 0 else 0
    
    # check_pair_pred_true_idx = (torch.cat(emo_cause_pred_y_list) % 10 > 0) # list 중에, 모델이 cause라고 prediction한 것만 True
    # check_pair_true_true_idx = (torch.cat(emo_cause_true_y_list) % 10 > 0) # list 중에, label이 cause인 것만 True
    # check_pair_true_true_idx_all = (torch.cat(emo_cause_true_y_list_all) % 10 > 0) # list 중에, label이 cause인 것만 True
    # check_pair_emotion_equal_idx = (torch.cat(emo_cause_pred_y_list) // 10 == torch.cat(emo_cause_true_y_list) // 10) # list 중에, 모델이 emotion이랑 label이랑 같은 것만 True
    # check_pair_emotion_equal_idx_all = (torch.cat(emo_cause_pred_y_list_all) // 10 == torch.cat(emo_cause_true_y_list_all) // 10) # list 중에, 모델이 emotion이랑 label이랑 같은 것만 True
    
    # emo_cau_TP = (check_pair_emotion_equal_idx&(check_pair_pred_true_idx & check_pair_true_true_idx)).sum()
    #     # 감정이 일치하는 것 중에서, pair가 label과 prediction 모두 pair라고 예측한 것
        
    # # Prediction에서, Emotion이 label과 일치하며 "Cause라고 예측한 개수"를 센다
    # emo_cau_TP_FP = (check_pair_pred_true_idx & check_pair_emotion_equal_idx).sum()
    
    # # Label에서, 전체 리스트 속, 감정이 일치하는 것 중에서 "True Pair" 개수를 센다
    # emo_cau_TP_FN = (check_pair_emotion_equal_idx_all & check_pair_true_true_idx_all).sum()
    
    # p_emo_cau = (emo_cau_TP / emo_cau_TP_FP) if emo_cau_TP_FP != 0 else 0
    # r_emo_cau = (emo_cau_TP / emo_cau_TP_FN) if emo_cau_TP_FN != 0 else 0
    # f1_emo_cau = 2 * p_emo_cau * r_emo_cau / (p_emo_cau + r_emo_cau) if p_emo_cau + r_emo_cau != 0 else 0
    
    return emo_report_str, emo_metrics, acc_cau, p_cau, r_cau, f1_cau, p_emo_cau, r_emo_cau, f1_emo_cau

def argmax_prediction(pred_y, true_y):
    pred_argmax = torch.argmax(pred_y, dim=1).cpu()
    true_y = true_y.cpu()
    return pred_argmax, true_y

def threshold_prediction(pred_y, true_y):
    pred_y = pred_y > 0.5
    return pred_y, true_y

def metrics_report(pred_y, true_y, label, get_dict=False, multilabel=False):
    true_y = true_y.view(-1)
    if multilabel:
        pred_y, true_y = threshold_prediction(pred_y, true_y)
        available_label = sorted(list(set((pred_y == True).nonzero()[:, -1].tolist() + (true_y == True).nonzero()[:, -1].tolist())))
    else:
        pred_y, true_y = argmax_prediction(pred_y, true_y)
        available_label = sorted(list(set(true_y.tolist() + pred_y.tolist())))

    class_name = list(label[available_label])
    if get_dict:
        return classification_report(true_y, pred_y, target_names=class_name, zero_division=0, digits=4, output_dict=True)
    else:
        return classification_report(true_y, pred_y, target_names=class_name, zero_division=0, digits=4)
