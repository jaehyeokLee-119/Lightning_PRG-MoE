import logging
import os
import datetime
import tensorflow
import lightning.pytorch as L
import torch
import torch.optim as optim
from tqdm import tqdm
from torch.utils.data import TensorDataset, DataLoader

from module.lightmodule import LitPRGMoE

from module.preprocessing import get_data

os.environ['CUDA_LAUNCH_BLOCKING'] = "1"
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3' # ignore TF error message 

class LearningEnv:
    def __init__(self, **kwargs):
        self.use_wandb = kwargs['use_wandb']
        self.wandb_project_name = kwargs['wandb_pjname']
        self.num_worker = kwargs['num_worker']
        # if self.use_wandb:
        #     wandb.init(project=self.wandb_project_name)
        self.only_emotion = kwargs['only_emotion']
        
        self.gpus = kwargs['gpus']
        self.single_gpu = len(self.gpus) == 1
        self.num_worker = kwargs['num_worker']

        self.train_dataset = kwargs['train_data']
        self.valid_dataset = kwargs['valid_data']
        self.test_dataset = kwargs['test_data']
        self.data_label = kwargs['data_label']

        self.max_seq_len = kwargs['max_seq_len']
        self.start_time = datetime.datetime.now()
        self.training_iter = kwargs['training_iter']
        
        self.model_name = kwargs['model_name']
        self.port = kwargs['port']
        
        self.pretrained_model = kwargs['pretrained_model']
        
        # Hyperparameters
        self.dropout = kwargs['dropout']
        self.n_cause = kwargs['n_cause']
        self.n_speaker = kwargs['n_speaker']
        self.n_emotion = kwargs['n_emotion']
        self.n_expert = kwargs['n_expert']
        self.encoder_name = kwargs['encoder_name']
        self.learning_rate = kwargs['learning_rate']
        self.unfreeze = kwargs['unfreeze']
        self.batch_size = kwargs['batch_size']
        self.guiding_lambda = kwargs['guiding_lambda']
        self.contain_context = kwargs['contain_context']
        self.encoder_separation = kwargs['encoder_separation']
        # learning variables
        self.best_performance = [0, 0, 0]  # p, r, f1
        self.num_epoch = 1
        
        # set log directory
        encodername_for_filename = self.encoder_name.replace('/', '_')
        separated_encoder = 'separated' if self.encoder_separation else 'not_separated'
        # directory for saving logs
        self.log_directory = f"{kwargs['log_directory']}/{encodername_for_filename}-{separated_encoder}_lr{self.learning_rate}_{self.data_label}"

        self.model_args = {
            "dropout": self.dropout,
            "n_speaker": self.n_speaker,
            "n_emotion": self.n_emotion,
            "n_cause": self.n_cause,
            "n_expert": self.n_expert,
            "guiding_lambda": self.guiding_lambda,
            "learning_rate": self.learning_rate,
            "encoder_name": self.encoder_name,
            "unfreeze": self.unfreeze,
            "only_emotion": self.only_emotion,
            "training_iter": self.training_iter,
            "encoder_separation": self.encoder_separation,
        }

    def set_model(self):
        # self.model = LitPRGMoE
        if self.pretrained_model is not None:
            model = LitPRGMoE(**self.model_args)
            model = model.load_from_checkpoint(self.pretrained_model)
        else:
            model = LitPRGMoE(**self.model_args)
        self.model = model
        
    
    def run(self, **kwargs):
        self.pre_setting()
        if kwargs['test']:
            self.training_iter = 1
            self.test()
        else:
            self.train()
    
    def pre_setting(self):
        encoder_name = self.encoder_name.replace('/', '_')
        
        EmotionText = 'OnlyEmotion' if self.only_emotion else ''
        # 로거 설정
        logger_name_list = ['train', 'valid', 'test']
        file_name_list = [f'{encoder_name}-{EmotionText}{self.data_label}-lr_{self.learning_rate}-Unfreeze{self.unfreeze}-{_}-{self.start_time}.log' for _ in logger_name_list]
        
        self.set_logger_environment(file_name_list, logger_name_list)
        
        # 모델 저장할 폴더 생성
        if not os.path.exists("model/"):
            os.makedirs("model/")
        
        # 모델 인스턴스를 셋팅
        self.set_model()
    
        
    def train(self):
        train_dataloader = self.get_dataloader(self.train_dataset, self.batch_size, self.num_worker, shuffle=True, contain_context=self.contain_context)
        valid_dataloader = self.get_dataloader(self.valid_dataset, self.batch_size, self.num_worker, shuffle=False, contain_context=self.contain_context)
        test_dataloader = self.get_dataloader(self.test_dataset, self.batch_size, self.num_worker, shuffle=False, contain_context=self.contain_context)
        
        # metrics = {"loss": "ptl/val_loss", "acc": "ptl/val_accuracy"}
        
        trainer_config = {
            "max_epochs": self.training_iter,
            "strategy": 'ddp_find_unused_parameters_true',
            "check_val_every_n_epoch": 1,
            "accumulate_grad_batches": 4,
            "default_root_dir": '/hdd/hjl8708/',
        }
        trainer = L.Trainer(**trainer_config)
        
        trainer.fit(self.model, train_dataloaders=train_dataloader, val_dataloaders=valid_dataloader)
        trainer.validate(self.model, dataloaders=valid_dataloader)
        trainer.test(self.model, dataloaders=test_dataloader)
    
    def test(self):
        test_dataloader = self.get_dataloader(self.test_dataset, self.batch_size, self.num_worker, shuffle=False, contain_context=self.contain_context)
        trainer = L.Trainer(max_epochs=self.training_iter, strategy='ddp_find_unused_parameters_true')
        trainer.test(self.model, dataloaders=test_dataloader)
    
    
    
    def set_logger_environment(self, file_name_list, logger_name_list):
        # logger_name_list = ['train', 'valid', 'test']
        # file_name_list = ['train_log.log', 'valid_log.log', 'test_log.log']
        for file_name, logger_name in zip(file_name_list, logger_name_list):
            for handler in logging.getLogger(logger_name).handlers[:]:
                logging.getLogger(logger_name).removeHandler(handler)
            self.set_logger(file_name, logger_name)

    def set_logger(self, file_name, logger_name):
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        if self.log_directory:
            if not os.path.exists(f'{self.log_directory}'):
                os.makedirs(f'{self.log_directory}')
            file_handler = logging.FileHandler(f'{self.log_directory}/{file_name}')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    def get_dataloader(self, dataset_file, batch_size, num_worker, shuffle=True, contain_context=False):
        device = "cuda:0"
        data = get_data(dataset_file, device, self.max_seq_len, self.encoder_name, contain_context)
        utterance_input_ids_t, utterance_attention_mask_t, utterance_token_type_ids_t = data[0]
        speaker_t, emotion_label_t, pair_cause_label_t, pair_binary_cause_label_t = data[1:]

        dataset_ = TensorDataset(utterance_input_ids_t, utterance_attention_mask_t, utterance_token_type_ids_t, speaker_t, emotion_label_t, pair_cause_label_t, pair_binary_cause_label_t)
        
        dataloader_params = {
            "dataset": dataset_,
            "batch_size": batch_size,
            "num_workers": num_worker,
            # "shuffle": shuffle
        }
        
        return DataLoader(**dataloader_params)
    
class ModelSaver:
    def __init__(self, path='checkpoint.pt', single_gpu=None):
        self.path = path
        self.single_gpu = single_gpu

    def __call__(self, model):
        state_dict = model.state_dict()
        torch.save(state_dict, self.path)