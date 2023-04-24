import main 
import torch
import os

if __name__ == "__main__":
    os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    torch.set_float32_matmul_precision('high')

    # Entire data (folds 먼저)
    train_data_list = [
        * [f'data/data_fold/data_{fold_}/data_{fold_}_train.json' for fold_ in range(1, 5)],
        'data/data_fold/data_0/dailydialog_train.json',
    ]
    valid_data_list = [
        * [f'data/data_fold/data_{fold_}/data_{fold_}_valid.json' for fold_ in range(1, 5)],
        'data/data_fold/data_0/dailydialog_valid.json',
    ]
    test_data_list = [
        * [f'data/data_fold/data_{fold_}/data_{fold_}_test.json' for fold_ in range(1, 5)],
        'data/data_fold/data_0/dailydialog_test.json',
    ]
    data_label = [*[f'-data_{fold_}_DailyDialog' for fold_ in range(1, 5)], '-original_data_DailyDialog']


    lr = [5e-5]
    batch_sizes = [5]
    gpus = [0]
    loss_lambda_list = [0.2, 1]
    accumulate_grad_batches = 1
    # emotion_encoder_name_list = ['j-hartmann/emotion-english-roberta-large'] , j-hartmann/emotion-english-distilroberta-base
    # cause_encoder_name_list = ['roberta-base']
    
        # encoder_name이 ORIGINAL이면, Original PRG-MoE(BertModel)를 사용하고, 아니면, 
        # 해당 이름의 모델(AutoModelForSequenceClassification)을 사용한다.
    encoder_name_list = ['j-hartmann/emotion-english-distilroberta-base','j-hartmann/emotion-english-distilroberta-base']
    mode = 'train'
    use_newfc_list = [True, False]
    
    ckpt_type = 'joint_accuracy' # 'cause-f1', 'emotion-f1', 'joint_accuracy'
        # 어떤 것이 높은 모델을 저장할 것인지
    
    if mode == 'train':
        for encoder_name, use_newfc in zip(encoder_name_list, use_newfc_list):
            for loss_lambda in loss_lambda_list:
                for tr, va, te, dl in zip(train_data_list, valid_data_list, test_data_list, data_label):
                    for lr_ in lr:
                        for batch_size in batch_sizes:
                            runner = main.Main()
                            runner.set_dataset(tr, va, te, dl)
                            runner.set_gpus(gpus)
                            runner.set_hyperparameters(learning_rate=lr_, batch_size=batch_size)
                            runner.set_value('training_iter', 20)
                            runner.set_value('encoder_name', encoder_name)
                            runner.set_value('accumulate_grad_batches', accumulate_grad_batches)
                            runner.set_value('loss_lambda', loss_lambda)
                            runner.set_value('ckpt_type', ckpt_type)
                            runner.set_value('use_newfc', use_newfc)
                            encoder_name_for_filename = encoder_name.replace('/', '-')
                            # runner.set_value('log_folder_name', f'Encoder_loss_lambda{loss_lambda}-{encoder_filename}_Total_Test_{dl}_batch{batch_size}')
                            runner.set_value('log_folder_name', f'(모델타입:BEST {ckpt_type}, UseNewFC-{use_newfc}-batch{batch_size},축적{accumulate_grad_batches}-losslambda{loss_lambda}){encoder_name_for_filename}_{dl}')
                            runner.run()
                            
                            del runner
    else: # test
        test_model_list = [
            # separated 0,1,2,3,4 / unseparated 5,6,7,8,9
            'model/bert-base-cased--original_data_DailyDialog-not_separated_lr_5e-05_2023-04-16 07:20:14.826287.ckpt',
            'model/bert-base-cased--data_1_DailyDialog-not_separated_lr_5e-05_2023-04-16 07:52:05.793635.ckpt',
            'model/bert-base-cased--data_2_DailyDialog-not_separated_lr_5e-05_2023-04-16 08:26:40.517597.ckpt',
            'model/bert-base-cased--data_3_DailyDialog-not_separated_lr_5e-05_2023-04-16 09:00:43.799727.ckpt',
            'model/bert-base-cased--data_4_DailyDialog-not_separated_lr_5e-05_2023-04-16 09:36:27.538797.ckpt',
        ]
        test_log_folder_list = [
            'test_bert-unseparated_at_original',
            'test_bert-unseparated_at_data1',
            'test_bert-unseparated_at_data2',
            'test_bert-unseparated_at_data3',
            'test_bert-unseparated_at_data4',
        ]
        for tr, va, te, dl, tm, tfn in zip(train_data_list, valid_data_list, test_data_list, 
                                      data_label, test_model_list, test_log_folder_list):
            runner = main.Main()
            runner.set_dataset(tr, va, te, dl)
            runner.set_gpus(gpus)
            runner.set_value('encoder_name', encoder_name)
            runner.set_test(ckpt_path=tm)
            runner.set_value('encoder_separation', False)
            runner.set_value('log_folder_name', tfn)
            runner.run()
            
            del runner
        