from transformers import (AdamW, OpenAIGPTDoubleHeadsModel, OpenAIGPTTokenizer,
                                  GPT2DoubleHeadsModel, GPT2Tokenizer, WEIGHTS_NAME, CONFIG_NAME)
from argparse import ArgumentParser
from tqdm import tqdm
from datetime import datetime
from functools import partial

from torch.utils.data import DataLoader
from models.reinforce_model.utils import get_dataset, make_logdir
from models.reinforce_model.data import PADDED_INPUTS, ATTR_TO_SPECIAL_TOKEN
from models.reinforce_model.dataset import PersonaChatDataset, collate_dialog, EFFECTS, MAX_NUM_COMET_PERSONA, MAX_NUM_PERSONA
from models.reinforce_model.train import add_special_tokens_
from models.reinforce_model.model import LatentMarginalizedModel

import torch
import math
import os

parser = ArgumentParser()
parser.add_argument("--dataset_path", type=str, default="", help="Path or url of the dataset. If empty download from S3.")
parser.add_argument("--dataset_cache", type=str, default='persona_comet_weak_label_preprocessed', help="Path or url of the dataset cache")
parser.add_argument("--model_checkpoint_dir", type=str, default="", help="Path, url or short name of the model")
parser.add_argument("--load_checkpoint_from", type=str, default="", help="Path, url or short name of the model")

parser.add_argument("--num_candidates", type=int, default=2, help="Number of candidates for training")
parser.add_argument("--max_history", type=int, default=2, help="Number of previous exchanges to keep in history")

parser.add_argument("--lm_coef", type=float, default=1.0, help="LM loss coefficient")
parser.add_argument("--mc_coef", type=float, default=1.0, help="Multiple-choice loss coefficient")
parser.add_argument("--max_norm", type=float, default=1.0, help="Clipping gradient norm")
# parser.add_argument("--n_epochs", type=int, default=3, help="Number of training epochs")
parser.add_argument("--personality_permutations", type=int, default=1, help="Number of permutations of personality sentences")
parser.add_argument("--eval_before_start", action='store_true', help="If true start with a first evaluation before training")
parser.add_argument("--device", type=str, default="cuda" if torch.cuda.is_available() else "cpu", help="Device (cuda or cpu)")
parser.add_argument("--fp16", type=str, default="", help="Set to O0, O1, O2 or O3 for fp16 training (see apex documentation)")
parser.add_argument("--local_rank", type=int, default=-1, help="Local rank for distributed training (-1: not distributed)")
parser.add_argument("--num_beams", type=int, default=5, help="Number of beams for comet expansion")
parser.add_argument("--test_run_num", type=int, default=-1, help="Datapoints to run with in a test run")
# parser.add_argument("--exp_name", type=str, default="", required=True, help="Provide an experiment name")
parser.add_argument("--do_train", action='store_true', help="Do training")
parser.add_argument("--do_eval", action='store_true', help="Do Evaluation")
parser.add_argument("--no_persona", action='store_true', help="No Persona Evaluation")
parser.add_argument("--no_comet_persona", action='store_true', help="No Persona Evaluation")
parser.add_argument("--training_type", type=str, default="", help="Marginalize or Reinforce")
parser.add_argument("--prior_model", type=str, default="bow", help="Prior model selection")

parser.add_argument("--interpret", action='store_true', help="Interpret")
parser.add_argument("--perplexity", action='store_true', help="Perplexity")
args = parser.parse_args()


args.distributed = (args.local_rank != -1) 

training_args = torch.load(os.path.join(args.model_checkpoint_dir, 'model_training_args.bin'))
training_args.training_type = 'marginalize' # to make sure we are marginalizing
training_args.entropy_regularize_prior_wt = 0.0
if not hasattr(training_args, 'use_structured_prior'):
    training_args.use_structured_prior=False
    print('Filled missing args.')
print('Loaded training args.')

print("Prepare tokenizer, pretrained model and optimizer.")
tokenizer_class = GPT2Tokenizer # cant use Autotokenizer because checkpoint could be a Path
tokenizer = tokenizer_class.from_pretrained('gpt2')

orig_num_tokens = len(tokenizer.encoder)
print('Tokenizer length: {}'.format(orig_num_tokens))
num_added_tokens = tokenizer.add_special_tokens(ATTR_TO_SPECIAL_TOKEN)
print('Tokenizer new length: {}'.format(len(tokenizer.encoder)))

model_class = GPT2DoubleHeadsModel
model = LatentMarginalizedModel(training_args, generator_class=model_class)
model.gpt2_model.resize_token_embeddings(new_num_tokens=orig_num_tokens + num_added_tokens)

model_checkpoint_path = os.path.join(args.model_checkpoint_dir, args.load_checkpoint_from)
model_weights = torch.load(
        model_checkpoint_path, map_location=lambda storage, loc: storage
    )
model.load_state_dict(model_weights, strict=True)
print('Loaded model weights from {}'.format(model_checkpoint_path))

model.to(args.device)

# Add special tokens if they are not already added
# add_special_tokens_(model, tokenizer)

print("Prepare datasets")
start = datetime.now()

val_dataset = PersonaChatDataset(training_args, tokenizer, split='valid')
if training_args.no_comet_persona:
    max_num_persona = MAX_NUM_PERSONA
else:
    max_num_persona = MAX_NUM_COMET_PERSONA

val_loader = DataLoader(
    val_dataset,
    shuffle=False,
    batch_size=1,
    collate_fn=partial(collate_dialog, max_num_persona=max_num_persona),
    pin_memory=True)

print('{} - Data loaded. Starting training'.format(datetime.now() - start))

num_correct = 0.0
num_examples = 0.0
ppls = []
losses = []
all_persona_from_joint = []
all_persona_from_prior = []
for i, batch in tqdm(enumerate(val_loader), total=len(val_loader)):
    model.eval()
    with torch.no_grad():
        batch = tuple(input_tensor.to(args.device) for input_tensor in batch)
        # print(tokenizer.decode(input_ids[0, -1, :].tolist()))
        # if we dont send labels to model, it doesnt return losses
        batch = tuple(input_tensor.to(args.device) for input_tensor in batch)
        input_ids, token_type_ids, lm_labels, mc_token_ids, mc_labels, persona, history, effects = batch
        
        if args.perplexity:
            (_), (_), (_), (marginal_lm_loss), (num_labels) = model(
                input_ids=input_ids,
                token_type_ids=token_type_ids,
                mc_token_ids=mc_token_ids,
                lm_labels=lm_labels,
                mc_labels=mc_labels,
                persona=persona,
                history=history
            )
            losses.append(marginal_lm_loss)

        if args.interpret:
            joint_probs = model(
                input_ids=input_ids,
                token_type_ids=token_type_ids,
                mc_token_ids=mc_token_ids,
                lm_labels=lm_labels,
                mc_labels=mc_labels,
                persona=persona,
                history=history,
                interpret=True,
            )
            persona_interpreted = torch.argmax(joint_probs, axis=-1).item()
            all_persona_from_joint.append(persona_interpreted)

            prior_z = model.prior_model.get_prob_z_given_H(persona, history)
            z = torch.argmax(prior_z, axis=1).item()
            all_persona_from_prior.append(z)

if args.perplexity:
    average_nll = sum(losses) / len(losses)
    ppl = math.exp(average_nll)
    print("Average Loss: {}".format(average_nll))
    print("Average PPL: {}".format(ppl))

# interpretability
# load dataset
if args.interpret:
    dataset = get_dataset(tokenizer, args.dataset_path, args.dataset_cache)['valid']
    if args.test_run_num > 0:
        dataset = dataset[:args.test_run_num]

    acc_joint = 0
    acc_prior = 0
    total_labels = 0
    utt_count = 0
    for d_i, dialog in tqdm(enumerate(dataset), total=len(dataset)):
        for i, utterance in enumerate(dialog["utterances"]):
            weak_label = dialog["weak_labels"][2*i + 1]
            if not training_args.no_comet_persona:
                weak_label_comet = dialog["weak_labels_comet"][2*i + 1]
            # making sure we are getting the weak labels for correct utterance
            if weak_label["sentence"] != utterance["candidates"][-1] and weak_label_comet["sentence"] != utterance["candidates"][-1]:
                print('ERROR!')
                print(weak_label["sentence"])
                print(utterance["candidates"][-1])
            

            if training_args.no_comet_persona:
                # collect persona weak labels
                persona_labels = []
                if len(weak_label["label_persona"]) > 0:
                    for l in weak_label["label_persona"]:
                        persona_labels.append(l["idx"])
                
                if persona_labels:
                    if all_persona_from_joint[utt_count] in persona_labels:
                        acc_joint += 1
                    if all_persona_from_prior[utt_count] in persona_labels:
                        acc_prior += 1
                    total_labels += 1 

            # COMET
            elif not training_args.no_comet_persona:
                comet_persona_labels = []
                if len(weak_label_comet["label_persona"]) > 0:
                    for match in weak_label_comet["label_persona"]:
                        comet_persona_labels.append(match[0]["persona_sent_id"])
                        comet_idx = EFFECTS[match[0]["comet_key"]]*training_args.num_beams + match[0]["beam_id"]
                        comet_persona_labels.append(comet_idx)
                
                if comet_persona_labels:
                    if all_persona_from_joint[utt_count] in comet_persona_labels:
                        acc_joint += 1
                    if all_persona_from_prior[utt_count] in comet_persona_labels:
                        acc_prior += 1
                    total_labels += 1

            utt_count += 1
            
    print('Interpretibility with joint: {}'.format(acc_joint/total_labels))
    print('Interpretibility with prior: {}'.format(acc_prior/total_labels))
'''
/data2/bodhi/projects/persona-dialog/models/persona_weak_sup/runs/Mar03_01-49-47_deepyeti_gpt2weak_sup_og_persona

python3 -m models.reinforce_model.eval --dataset_path=/data3/bodhi/data/personachat/weak_label_comet_personachat/personachat_self_original_comet_scores_alignlabels.expanded_persona_preprocessed.json --model_checkpoint_dir=/data3/bodhi/projects/persona-dialog/models/reinforce_model/runs/Apr10_05-33-15_deepx_gpt2prior_bow_rl0.8_NEW/ --load_checkpoint_from=checkpoint_mymodel_43470.pth --lm_coef=2.0 --mc_coef=0.0 --max_history=2 --num_candidates=1 --personality_permutations=1 --valid_batch_size=1 --no_comet_persona --training_type=marginalize --test_run_num 30

w comet
Apr11_04-02-25_deepx_gpt2prior_bow_rl0.8_comet_NEW
checkpoint_mymodel_43470.pth



'''