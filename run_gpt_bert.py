import os

import argparse
import json
import logging
import math
import os
import random
from pathlib import Path
import numpy as np
import datasets
import evaluate
import torch
import matplotlib.pyplot as plt
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import set_seed
from datasets import load_dataset
from huggingface_hub import Repository, create_repo
from torch.utils.data import DataLoader
from tqdm.auto import tqdm
import copy

import transformers
from transformers import (
    AutoConfig,
    AutoModelForCausalLM,
    AutoTokenizer,
    DataCollatorWithPadding,
    PretrainedConfig,
    SchedulerType,
    default_data_collator,
    get_scheduler,
    LlamaForCausalLM, LlamaTokenizer,
    BertForSequenceClassification,
    AutoModelForSequenceClassification
)
from transformers.utils import check_min_version, get_full_repo_name, send_example_telemetry
from transformers.utils.versions import require_version

from peft import (
    get_peft_config,
    get_peft_model,
    get_peft_model_state_dict,
    set_peft_model_state_dict,
    LoraConfig,
    PeftType,
    PrefixTuningConfig,
    PromptEncoderConfig,
)

import preprocessing
from memory import save_gpu_stats 

logger = get_logger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="Finetune a transformers model on a text classification task")
    parser.add_argument(
        "--task_name",
        type=str,
        default='winogrande_s',
        help="The name of the glue task to train on.",
    )
    parser.add_argument(
        "--train_file", type=str, default=None, help="A csv or a json file containing the training data."
    )
    parser.add_argument(
        "--validation_file", type=str, default=None, help="A csv or a json file containing the validation data."
    )
    parser.add_argument(
        "--max_length",
        type=int,
        default=400,
        help=(
            "The maximum total input sequence length after tokenization. Sequences longer than this will be truncated,"
            " sequences shorter will be padded if `--pad_to_max_length` is passed."
        ),
    )
    parser.add_argument(
        "--pad_to_max_length",
        action="store_true",
        help="If passed, pad all samples to `max_length`. Otherwise, dynamic padding is used.",
    )
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        default='meta-llama/Llama-2-7b-hf',
        help="Path to pretrained model or model identifier from huggingface.co/models."
    )
    parser.add_argument(
        "--use_slow_tokenizer",
        action="store_true",
        help="If passed, will use a slow tokenizer (not backed by the 🤗 Tokenizers library).",
    )
    parser.add_argument(
        "--per_device_train_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the training dataloader.",
    )
    parser.add_argument(
        "--per_device_eval_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the evaluation dataloader.",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=5e-5,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument(
        "--max_grad_norm",
        type=float,
        default=.5,
        help="Gradient clipping norm.",
    )
    parser.add_argument("--weight_decay", type=float, default=0.0, help="Weight decay to use.")
    parser.add_argument("--num_train_epochs", type=int, default=3, help="Total number of training epochs to perform.")
    parser.add_argument(
        "--max_train_steps",
        type=int,
        default=None,
        help="Total number of training steps to perform. If provided, overrides num_train_epochs.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--lr_scheduler_type",
        type=SchedulerType,
        default="linear",
        help="The scheduler type to use.",
        choices=["linear", "cosine", "cosine_with_restarts", "polynomial", "constant", "constant_with_warmup"],
    )
    parser.add_argument(
        "--num_warmup_steps", type=int, default=0, help="Number of steps for the warmup in the lr scheduler."
    )
    parser.add_argument("--output_dir", type=str, default='./outputs', help="Where to store the final model.")
    parser.add_argument("--seed", type=int, default=0, help="A seed for reproducible training.")
    parser.add_argument("--push_to_hub", action="store_true", help="Whether or not to push the model to the Hub.")
    parser.add_argument(
        "--hub_model_id", type=str, help="The name of the repository to keep in sync with the local `output_dir`."
    )
    parser.add_argument("--hub_token", type=str, help="The token to use to push to the Model Hub.")
    parser.add_argument(
        "--checkpointing_steps",
        type=str,
        default=None,
        help="Whether the various states should be saved at the end of every n steps, or 'epoch' for each epoch.",
    )
    parser.add_argument(
        "--resume_from_checkpoint",
        type=str,
        default=None,
        help="If the training should continue from a checkpoint folder.",
    )
    parser.add_argument(
        "--with_tracking",
        action="store_true",
        help="Whether to enable experiment trackers for logging.",
    )
    parser.add_argument(
        "--report_to",
        type=str,
        default="all",
        help=(
            'The integration to report the results and logs to. Supported platforms are `"tensorboard"`,'
            ' `"wandb"`, `"comet_ml"` and `"clearml"`. Use `"all"` (default) to report to all integrations.'
            "Only applicable when `--with_tracking` is passed."
        ),
    )
    parser.add_argument(
        "--ignore_mismatched_sizes",
        action="store_true",
        default=True,
        help="Whether or not to enable to load a pretrained model whose head dimensions are different.",
    )
    parser.add_argument(
        "--save_train_results",
        action="store_true",
        default=False,
        help="Whether or not to save evaluation on training set.",
    )
    parser.add_argument("--lora_r", type=int, default=8)
    parser.add_argument("--lora_alpha", type=int, default=16)
    parser.add_argument("--lora_dropout", type=float, default=0.1)
    parser.add_argument("--testing_set", type=str, default='val')
    parser.add_argument("--lm_head", action="store_true", default=False)
    args = parser.parse_args()

    print(args)

    peft_method = 'lora'
    if args.lm_head:
        peft_method = 'lora_lmhead'
    if args.testing_set != 'val':
        peft_method += args.testing_set

    
    args.output_dir += f'/{args.task_name}/{args.model_name_or_path}_{peft_method}_{args.lora_r}_{args.lora_alpha}_{args.lora_dropout}_{args.learning_rate}_{args.seed}_{args.per_device_train_batch_size}_{args.max_train_steps}'


    os.makedirs(args.output_dir, exist_ok=True)

    args_file_path = os.path.join(args.output_dir, 'args.json')
    args_dict = vars(args)
    with open(args_file_path, 'w+') as f:
      json.dump(args_dict, f, indent=4)

    # Sanity checks
    if args.task_name is None and args.train_file is None and args.validation_file is None:
        raise ValueError("Need either a task name or a training/validation file.")
    else:
        if args.train_file is not None:
            extension = args.train_file.split(".")[-1]
            assert extension in ["csv", "json"], "`train_file` should be a csv or a json file."
        if args.validation_file is not None:
            extension = args.validation_file.split(".")[-1]
            assert extension in ["csv", "json"], "`validation_file` should be a csv or a json file."

    if args.push_to_hub:
        assert args.output_dir is not None, "Need an `output_dir` to create a repo when `--push_to_hub` is passed."

    return args


def main():
    args = parse_args()
    # Sending telemetry. Tracking the example usage helps us better allocate resources to maintain them. The
    # information sent is the one passed as arguments along with your Python/PyTorch versions.
    send_example_telemetry("run_glue_no_trainer", args)

    # Initialize the accelerator. We will let the accelerator handle device placement for us in this example.
    # If we're using tracking, we also need to initialize it here and it will by default pick up all supported trackers
    # in the environment
    accelerator = (
        Accelerator(log_with=args.report_to, project_dir=args.output_dir) if args.with_tracking else Accelerator()
    )
    # Make one log on every process with the configuration for debugging.
    log_file_path = os.path.join(args.output_dir, 'logfile.log')

    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
        filename=log_file_path
    )
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    console_handler.setFormatter(formatter)

    logger = logging.getLogger(__name__)
    logger.addHandler(console_handler)
    #logger.info(accelerator.state, main_process_only=False)
    if accelerator.is_local_main_process:
        datasets.utils.logging.set_verbosity_warning()
        transformers.utils.logging.set_verbosity_info()
    else:
        datasets.utils.logging.set_verbosity_error()
        transformers.utils.logging.set_verbosity_error()

    # If passed along, set the training seed now.
    if args.seed is not None:
        set_seed(args.seed)

    # Handle the repository creation
    if accelerator.is_main_process:
        if args.push_to_hub:
            if args.hub_model_id is None:
                repo_name = get_full_repo_name(Path(args.output_dir).name, token=args.hub_token)
            else:
                repo_name = args.hub_model_id
            create_repo(repo_name, exist_ok=True, token=args.hub_token)
            repo = Repository(args.output_dir, clone_from=repo_name, token=args.hub_token)

            with open(os.path.join(args.output_dir, ".gitignore"), "w+") as gitignore:
                if "step_*" not in gitignore:
                    gitignore.write("step_*\n")
                if "epoch_*" not in gitignore:
                    gitignore.write("epoch_*\n")
        elif args.output_dir is not None:
            os.makedirs(args.output_dir, exist_ok=True)
    accelerator.wait_for_everyone()

    # Get the datasets: you can either provide your own CSV/JSON training and evaluation files (see below)
    # or specify a GLUE benchmark task (the dataset will be downloaded automatically from the datasets Hub).

    # For CSV/JSON files, this script will use as labels the column called 'label' and as pair of sentences the
    # sentences in columns called 'sentence1' and 'sentence2' if such column exists or the first two columns not named
    # label if at least two columns are provided.

    # If the CSVs/JSONs contain only one non-label column, the script does single sentence classification on this
    # single column. You can easily tweak this behavior (see below)

    # In distributed training, the load_dataset function guarantee that only one local process can concurrently
    # download the dataset.
    cache_dir = "/content/cache/huggingface" 
    os.makedirs(cache_dir, exist_ok=True)
    os.environ["HF_HOME"] = cache_dir

    raw_datasets,num_labels= preprocessing.download_data(args,cache_dir)

    logger.info(f" Number of labels detected = {num_labels}")

    is_regression = args.task_name.lower() == "stsb"
    if 'stsb' in args.task_name:
      num_labels = 1
    # Load pretrained model and tokenizer
    #
    # In distributed training, the .from_pretrained methods guarantee that only one local process can concurrently
    # download model & vocab.
    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path, use_fast=not args.use_slow_tokenizer, padding_side='left', use_auth_token='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')
    
    #tokenizer.pad_token = tokenizer.eos_token
    if args.task_name in ['boolq']:  #,'winogrande_m', 'winogrande_s']:
        tokenizer.add_eos_token = True
    
    model = AutoModelForSequenceClassification.from_pretrained(
        args.model_name_or_path, load_in_8bit=False, num_labels = num_labels #True
    )


    
    target_modules=['query','value'] 
    #if args.lm_head:
    #    if 'roberta' in args.model_name_or_path:
    #        target_modules.append('classifier.dense')
    #        target_modules.append('classifier.out_proj')
    #    elif 'bert' in args.model_name_or_path:
    #        target_modules.append('classifier')
    peft_config = LoraConfig(task_type="SEQ_CLS", inference_mode=False, r=args.lora_r, lora_alpha=args.lora_alpha, lora_dropout=args.lora_dropout, target_modules=target_modules)
    model = get_peft_model(model, peft_config)
    logger.info(model.print_trainable_parameters())
    #print(model)

    #for name, module in model.named_modules():
    #    if 'lora' in name.lower():  # Adjust the condition based on your naming convention
    #        print(name)
    
    #for name, param in model.named_parameters():
        #param.requires_grad = False
        #if 'lora' in name.lower():
            #param.requires_grad = True
        #if param.requires_grad:
            #print(name)

    padding = "max_length" if args.pad_to_max_length else False

    processed_datasets = raw_datasets.map(
    lambda examples: preprocessing.preprocess_function(examples,tokenizer,args,padding),
    batched=True,
    remove_columns=raw_datasets["train"].column_names,
    desc="Running tokenizer on dataset",
    )

    # print('====train data====')
    train_dataset = processed_datasets["train"]
    # print('====validation data====')
    processed_dataset = processed_datasets["validation_matched" if args.task_name == "mnli" else "validation"]

    if args.testing_set == 'test':
        ds = processed_dataset.train_test_split(test_size=0.5, seed=42, shuffle=False)
        val_dataset, eval_dataset = ds["train"], ds["test"]
    elif args.testing_set == 'train_val':
        ds = train_dataset.train_test_split(test_size=0.2, seed=42, shuffle=False)
        train_dataset, val_dataset = ds["train"], ds["test"]
        eval_dataset = processed_dataset
    elif args.testing_set == 'val':
        eval_dataset = processed_dataset

    # Log a few random samples from the training set:
    for index in random.sample(range(len(train_dataset)), 3):
        logger.info(f"Sample {index} of the training set: {train_dataset[index]}.")

    # DataLoaders creation:
    if args.pad_to_max_length:
        # If padding was already done ot max length, we use the default data collator that will just convert everything
        # to tensors.
        data_collator = default_data_collator
    else:
        # Otherwise, `DataCollatorWithPadding` will apply dynamic padding for us (by padding to the maximum length of
        # the samples passed). When using mixed precision, we add `pad_to_multiple_of=8` to pad all tensors to multiple
        # of 8s, which will enable the use of Tensor Cores on NVIDIA hardware with compute capability >= 7.5 (Volta).
        data_collator = DataCollatorWithPadding(tokenizer, pad_to_multiple_of=(8 if accelerator.use_fp16 else None))

    train_dataloader = DataLoader(
        train_dataset, shuffle=True, collate_fn=data_collator, batch_size=args.per_device_train_batch_size
    )
    eval_dataloader = DataLoader(eval_dataset, collate_fn=data_collator, batch_size=args.per_device_eval_batch_size)

    if args.testing_set != 'val':
        val_dataloader = DataLoader(val_dataset, collate_fn=data_collator, batch_size=args.per_device_eval_batch_size)


    # Optimizer
    # Split weights in two groups, one with weight decay and the other not.
    logger.info(f" Max training steps before recalculation = {args.max_train_steps}")
    overrode_max_train_steps = False
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    logger.info(f" num_update_steps_per_epoch initial = {num_update_steps_per_epoch}")
    logger.info(f" num training epochs initial = {args.num_train_epochs}")
    if args.max_train_steps is None:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
        overrode_max_train_steps = True
   
    # Scheduler and math around the number of training steps.
    #logger.info(f" Max training steps before recalculation = {args.max_train_steps}")
    #overrode_max_train_steps = False
    #num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    #logger.info(f" num_update_steps_per_epoch initial = {num_update_steps_per_epoch}")
    #logger.info(f" num training epochs initial = {args.num_train_epochs}")
    #if args.max_train_steps is not None:
      #  args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)
    #logger.info(f"Adjusted num_train_epochs based on max_train_steps: {args.num_train_epochs}")

    #if args.max_train_steps is None:
        #args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
        #overrode_max_train_steps = True
    logger.info(f"Adjusted num_train_epochs based on max_train_steps: {args.num_train_epochs}")



    # Prepare everything with our `accelerator`.

    
    class WrappedModel(torch.nn.Module):
        def __init__(self, model):
            super().__init__()

            if args.task_name == 'boolq':
                self.id_list = [tokenizer.encode('False')[1], tokenizer.encode('True')[1]]
            elif args.task_name == 'openbookqa':
                self.id_list = [tokenizer.encode('A')[1], tokenizer.encode('B')[1], tokenizer.encode('C')[1], tokenizer.encode('D')[1]]
            elif 'ARC' in args.task_name:
                self.id_list = [tokenizer.encode('A')[1], tokenizer.encode('B')[1], tokenizer.encode('C')[1], tokenizer.encode('D')[1]]
            elif 'winogrande' in args.task_name:
                self.id_list = [tokenizer.encode('A')[1], tokenizer.encode('B')[1]]

            self.model = model


        #def forward(self, **kwargs):
            #kwargs.pop('labels', None)
            #output_dict = self.model(**kwargs)
            #logits = output_dict['logits']
            #need to make changes for GLUE tasks
            #selected_logits = logits[:, -1, self.id_list]
            #output_dict['logits'] = selected_logits
            #return output_dict

            # Convert logits to probabilities using softmax
            #probs = torch.softmax(logits, dim=-1)
    
            # Convert probabilities to class predictions
            #predictions = torch.argmax(probs, dim=-1)
    
            # Update the output dictionary to include class predictions
            #output_dict['predictions'] = predictions

            #return output_dict

    

    #model = WrappedModel(model)

    logger.info(model)

    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if p.requires_grad and not any(nd in n for nd in no_decay)],
            "weight_decay": args.weight_decay,
        },
        {
            "params": [p for n, p in model.named_parameters() if p.requires_grad and any(nd in n for nd in no_decay)],
            "weight_decay": 0.0,
        },
    ]
    optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=args.learning_rate)

    lr_scheduler = get_scheduler(
        name=args.lr_scheduler_type,
        optimizer=optimizer,
        num_warmup_steps=args.num_warmup_steps,
        num_training_steps=args.max_train_steps,
    )

    if args.testing_set == 'train_val':
          model, optimizer, train_dataloader, eval_dataloader, val_dataloader,lr_scheduler = accelerator.prepare(
        model, optimizer, train_dataloader, eval_dataloader, val_dataloader, lr_scheduler
        )
    else:
          model, optimizer, train_dataloader, eval_dataloader, lr_scheduler = accelerator.prepare(
        model, optimizer, train_dataloader, eval_dataloader, lr_scheduler
       )

    # We need to recalculate our total training steps as the size of the training dataloader may have changed
    logger.info(f" num_update_steps_per_epoch before recalculation = {num_update_steps_per_epoch}")
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    logger.info(f" num_update_steps_per_epoch after recalculation = {num_update_steps_per_epoch}")
    if overrode_max_train_steps:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
    # Afterwards we recalculate our number of training epochs
    args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)
    logger.info(f" num training epochs before recalculation = {args.num_train_epochs}")

    #logger.info(f" num_update_steps_per_epoch before recalculation = {num_update_steps_per_epoch}")
    # We need to recalculate our total training steps as the size of the training dataloader may have changed
    #num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    #logger.info(f" num_update_steps_per_epoch after recalculation = {num_update_steps_per_epoch}")
    #if overrode_max_train_steps:
    #    args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
    # Afterwards we recalculate our number of training epochs
    #logger.info(f" num training epochs before recalculation = {args.num_train_epochs}")
    #args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)
    #logger.info(f" num training epochs after recalculation = {args.num_train_epochs}")
    #logger.info(f" Max training steps after recalculation = {args.max_train_steps}")
    
    #logger.info(f"Final num_train_epochs: {args.num_train_epochs}")
    #logger.info(f"Final max_train_steps: {args.max_train_steps}")

    # Figure out how many steps we should save the Accelerator states
    checkpointing_steps = args.checkpointing_steps
    #if checkpointing_steps is not None and checkpointing_steps.isdigit():
        #checkpointing_steps = int(checkpointing_steps)
    if args.checkpointing_steps is None:
        checkpointing_steps = np.arange(0, args.max_train_steps, (0.2 * args.max_train_steps)).astype(int).tolist()

        if checkpointing_steps[-1] != args.max_train_steps:
          checkpointing_steps.append(args.max_train_steps)
        print(checkpointing_steps)
    else:
        checkpointing_steps = args.checkpointing_steps
        if checkpointing_steps is not None and checkpointing_steps.isdigit():
            checkpointing_steps = int(checkpointing_steps)
        print(checkpointing_steps)

    # We need to initialize the trackers we use, and also store our configuration.
    # The trackers initializes automatically on the main process.
    if args.with_tracking:
        experiment_config = vars(args)
        # TensorBoard cannot log Enums, need the raw value
        experiment_config["lr_scheduler_type"] = experiment_config["lr_scheduler_type"].value
        accelerator.init_trackers("glue_no_trainer", experiment_config)

    # Get the metric function
    if args.task_name is not None:
        if args.task_name in ['wnli', 'rte', 'mrpc', 'cola', 'sst2', 'qnli', 'qqp', 'mnli','stsb']:
            metric = evaluate.load("glue", args.task_name, experiment_id=f"{args.output_dir}")
        elif args.task_name in ['cb', 'wic', 'boolq']:
            metric = evaluate.load("super_glue", args.task_name, experiment_id=f"{args.output_dir}")
        else:
            metric = evaluate.load('accuracy', experiment_id=f"{args.output_dir}")
    else:
        metric = evaluate.load("accuracy")

    # Train!
    total_batch_size = args.per_device_train_batch_size * accelerator.num_processes * args.gradient_accumulation_steps

    logger.info("***** Running training *****")
    logger.info(f"  Num examples = {len(train_dataset)}")
    logger.info(f"  Num Epochs = {args.num_train_epochs}")
    logger.info(f"  Instantaneous batch size per device = {args.per_device_train_batch_size}")
    logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
    logger.info(f"  Total optimization steps = {args.max_train_steps}")
    # Only show the progress bar once on each machine.
    progress_bar = tqdm(range(args.max_train_steps), disable=not accelerator.is_local_main_process)
    completed_steps = 0
    starting_epoch = 0
    # Potentially load in the weights and states from a previous save
    if args.resume_from_checkpoint:
        if args.resume_from_checkpoint is not None or args.resume_from_checkpoint != "":
            accelerator.print(f"Resumed from checkpoint: {args.resume_from_checkpoint}")
            accelerator.load_state(args.resume_from_checkpoint)
            path = os.path.basename(args.resume_from_checkpoint)
        else:
            # Get the most recent checkpoint
            dirs = [f.name for f in os.scandir(os.getcwd()) if f.is_dir()]
            dirs.sort(key=os.path.getctime)
            path = dirs[-1]  # Sorts folders by date modified, most recent checkpoint is the last
        # Extract `epoch_{i}` or `step_{i}`
        training_difference = os.path.splitext(path)[0]

        if "epoch" in training_difference:
            starting_epoch = int(training_difference.replace("epoch_", "")) + 1
            resume_step = None
            completed_steps = starting_epoch * num_update_steps_per_epoch
        else:
            # need to multiply `gradient_accumulation_steps` to reflect real steps
            resume_step = int(training_difference.replace("step_", "")) * args.gradient_accumulation_steps
            starting_epoch = resume_step // len(train_dataloader)
            resume_step -= starting_epoch * len(train_dataloader)
            completed_steps = resume_step // args.gradient_accumulation_step

    # update the progress_bar if load from checkpoint
    progress_bar.update(completed_steps)

    test_loader_list = [eval_dataloader]
    test_loader_names = ['eval']
    if args.testing_set != 'val':
        test_loader_list.append(val_dataloader)
        test_loader_names.append('val')

    print('-------------Before Training loop ---------')
    
    for name, param in model.named_parameters():
        if param.requires_grad:
            print(f"{name}: {param.shape}")
    
    step_list = []
    train_losses = []
    val_losses = []

    for epoch in range(starting_epoch, args.num_train_epochs):
        active_dataloader = train_dataloader
        total_train_loss = 0
        for step, train_batch in enumerate(active_dataloader):
            
            if isinstance(checkpointing_steps, int):
                #print(f'Step : {completed_steps}')
                for test_loader, test_loader_name in zip(test_loader_list, test_loader_names):
                    if (completed_steps+1) % checkpointing_steps == 0 or completed_steps == 0:
                        output_dir = f"step_{completed_steps}"
                        if completed_steps not in step_list:
                            step_list.append(completed_steps)
                        print(step_list)
                        if args.output_dir is not None:
                            output_dir = os.path.join(args.output_dir, output_dir)

                        model.eval()
                        samples_seen = 0
                        output_dicts = []
                        total_val_losses = 0
                        for step, batch in tqdm(enumerate(test_loader),mininterval = 1,maxinterval=5):
                            with torch.no_grad():
                                outputs = model(**batch)
                            predictions = outputs.logits.argmax(dim=-1) if not is_regression else outputs.logits.squeeze()
                            y = batch['labels']
                            #logger.info(f' outputs shape : {predictions.shape}')
                            #logger.info(f'y shape : {y.shape}')
                            loss = torch.nn.CrossEntropyLoss()(outputs.logits, y) if not is_regression else torch.nn.MSELoss()(outputs.logits.squeeze(), y)
                            total_val_losses += loss.detach().cpu().float()

                            logits = outputs.logits.detach()
                            for j in range(logits.size(0)):
                                probs = logits[j]  #F.softmax(logits[j], -1)
                                label = batch["labels"]
                                output_dict = {
                                    'index': args.per_device_eval_batch_size * step + j,
                                    'true': label[j].item(),
                                    'pred': logits[j].argmax().item(),
                                    'conf': probs.max().item(),
                                    'logits': logits[j].cpu().numpy().tolist(),
                                    'probs': probs.cpu().numpy().tolist(),
                                }
                                output_dicts.append(output_dict)

                            predictions, references = accelerator.gather((predictions, batch["labels"]))
                            # If we are in a multiprocess environment, the last batch has duplicates
                            if accelerator.num_processes > 1:
                                if step == len(eval_dataloader) - 1:
                                    predictions = predictions[: len(eval_dataloader.dataset) - samples_seen]
                                    references = references[: len(eval_dataloader.dataset) - samples_seen]
                                else:
                                    samples_seen += references.shape[0]
                            metric.add_batch(
                                predictions=predictions,
                                references=references,
                            )

                        eval_metric = metric.compute()
                        logger.info(f"epoch {epoch}: {eval_metric}")

                        if test_loader_name == 'val':
                            avg_val_loss = total_val_losses/ len(test_loader)
                            val_losses.append(avg_val_loss)

                        if test_loader_name == 'eval':
                            accelerator.wait_for_everyone()
                            unwrapped_model = accelerator.unwrap_model(model)
                            unwrapped_model.save_pretrained(
                                output_dir, is_main_process=accelerator.is_main_process, save_function=accelerator.save
                            )
                            if accelerator.is_main_process:
                                tokenizer.save_pretrained(output_dir)
                    
                        all_results = {f"eval_{k}": v for k, v in eval_metric.items()}

                        gpu_dict = save_gpu_stats()
                        
                        if test_loader_name == 'eval':
                            all_results_output_path = os.path.join(output_dir, f"all_results.json")
                        elif test_loader_name == 'val':
                            all_results_output_path = os.path.join(output_dir, f"all_results_val.json")

                        if os.path.isfile(all_results_output_path):
                            os.remove(all_results_output_path)

                        with open(all_results_output_path, "w") as f:
                            json.dump(all_results, f)

                        if test_loader_name == 'eval':
                            output_path = os.path.join(output_dir, f'eval_res.json')
                        elif test_loader_name == 'val':
                            output_path = os.path.join(output_dir, f'val_res.json')
                        print(f'writing outputs to \'{output_path}\'')

                        if os.path.isfile(output_path):
                            os.remove(output_path)

                        with open(output_path, 'w+') as f:
                            for i, output_dict in enumerate(output_dicts):
                                output_dict_str = json.dumps(output_dict)
                                f.write(f'{output_dict_str}\n')
                        
                        # Write GPU statistics to a JSON file
                        output_path = os.path.join(output_dir, f'gpu_stats.json')
                        with open(output_path, "w+") as f:
                            json.dump(gpu_dict, f, indent=4)

                        steps_file_path = os.path.join(args.output_dir, 'steps.json')
                        with open(steps_file_path, 'w+') as f:
                          json.dump(step_list, f, indent=4)
                        
                        del output_dicts, all_results, output_dict, eval_metric, logits, probs, label, predictions, references, outputs
              
                if completed_steps > args.max_train_steps:
                  break

                model.train()
                outputs = model(**train_batch)
                y = train_batch['labels']
                #logger.info(f' outputs shape : {outputs.shape}')
                #logger.info(f'y shape : {y.shape}')
                loss = torch.nn.CrossEntropyLoss()(outputs.logits, y) if not is_regression else torch.nn.MSELoss()(outputs.logits.squeeze(), y)
                #loss = outputs.loss
                # We keep track of the loss at each epoch
                if args.with_tracking:
                    total_loss += loss.detach().cpu().float()
                loss = loss / args.gradient_accumulation_steps
                total_train_loss += loss.detach().cpu().float()
                #print(loss)
                accelerator.backward(loss)
                if step % args.gradient_accumulation_steps == 0 or step == len(train_dataloader) - 1:
                    optimizer.step()
                    lr_scheduler.step()
                    optimizer.zero_grad()
                    progress_bar.update(1)
                    completed_steps += 1

        avg_train_loss = total_train_loss / len(active_dataloader)
        #if (completed_steps+1) in checkpointing_steps or completed_steps == 0:
        train_losses.append(avg_train_loss)

    logger.info("***** Completed training *****")

    save_path = os.path.join(args.output_dir, f'{args.task_name}_{args.model_name_or_path}_validation_loss.png')
    #plt.plot(train_losses[::2], label='Train Loss')
    plt.plot(val_losses, label='Validation Loss')
    plt.xlabel('Epochs')
    plt.ylabel('Loss')
    plt.legend()
    plt.savefig(save_path)
    plt.show()

    save_path = os.path.join(args.output_dir, f'{args.task_name}_{args.model_name_or_path}_train_loss.png')
    plt.plot(train_losses, label='train Loss')
    plt.xlabel('Epochs')
    plt.ylabel('Loss')
    plt.legend()
    plt.savefig(save_path)
    plt.show()         

    


if __name__ == "__main__":
    main()