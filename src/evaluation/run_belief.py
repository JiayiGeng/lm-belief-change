import os
import time
import json
from pathlib import Path
import yaml
import fire
from rich import print
from src.core.models import Model
from src.core.utils import GenerationManager, parse_eval_output


WORKING_DIR = os.getcwd()
MULTITURN_DATA_DIR = f'{WORKING_DIR}/data/multiturn'
MULTITURN_PROMPTS_PATH = f'{WORKING_DIR}/src/prompts/multiturn.yaml'
STUDY_PROMPTS_PATH = f'{WORKING_DIR}/src/prompts/study.yaml'
STUDY_TOPICS_PATH = f'{WORKING_DIR}/data/study/topics.yaml'


def main(exp_name, **kwargs):
    # #########################################################################
    # General experiment args
    seed = kwargs.get('seed', 0)
    stage2_run_dir = kwargs.get('stage2_run_dir', 'none')
    run_dir = kwargs.get('run_dir', None)
    assert run_dir is not None, 'run_dir is required'

    exp_config = dict(
        exp_name=exp_name,
        seed=seed,
        run_dir=run_dir,
        stage2_run_dir=stage2_run_dir,
    )
    
    # Model args
    model_name = kwargs.get('model_name', 'gpt-4o')
    model = Model(model_name)
    ###########################################################################

    # Prepare data and prompts for in-depth reading and deep research
    if exp_name == 'study':
        # Data & prompts args
        with open(STUDY_PROMPTS_PATH, 'r') as f:
            PROMPTS = yaml.safe_load(f)
        with open(STUDY_TOPICS_PATH, 'r') as f:
            TOPICS = yaml.safe_load(f)
    
        survey_topic_index = kwargs.get('survey_topic_index', 0)
        survey_topic_name = TOPICS['survey'][survey_topic_index]['topic_name']
        support_statement = TOPICS['survey'][survey_topic_index]['support_statement']
        oppose_statement = TOPICS['survey'][survey_topic_index]['oppose_statement']
        
        input_text = PROMPTS['belief'].format(
            topic_name=survey_topic_name,
            support_statement=support_statement,
            oppose_statement=oppose_statement,
        )
        
        if stage2_run_dir != 'none':
            with open(Path(stage2_run_dir) / 'message_history.json', 'r') as f:
                message_history = json.load(f)
        else:
            message_history = []
    
        data_config = dict(
            survey_topic_index=survey_topic_index,
            topics=TOPICS,
            prompts=PROMPTS,
        )
    
    # Prepare data and prompts for multiturn interaction
    elif exp_name == 'multiturn': 
        with open(MULTITURN_PROMPTS_PATH, 'r') as f:
            PROMPTS = yaml.safe_load(f)
        
        dataset_name = kwargs.get('dataset_name', 'moral')
        model_type = kwargs.get('model_type', 'open')
        dataset_path = f'{MULTITURN_DATA_DIR}/{dataset_name}/disagreement_{model_type}data.jsonl'
        with open(dataset_path, 'r') as f:
            dataset = [json.loads(line) for line in f]
        
        query_index = kwargs.get('query_index', 0)
        datapoint = dataset[query_index]['datapoint']
        query = datapoint['moral_dilemma'] if dataset_name == "moral" else datapoint['query']
        statements = datapoint['statements']
        support_statement = statements['support_statement']
        oppose_statement = statements['oppose_statement']
        
        input_text = PROMPTS['moral_label'].format(
            moral_dilemma=query, 
            oppose_statement=oppose_statement,
            support_statement=support_statement,
        ) if dataset_name == "moral" else PROMPTS['safety_label'].format(
            query=query,
            oppose_statement=oppose_statement,
            support_statement=support_statement,
        )
        
        if stage2_run_dir != 'none':
            rounds_num = kwargs.get('rounds_num', 10)
            mode = kwargs.get('mode', 'two-sided')
            if mode == "two-sided":
                if model_name == "azure/gpt-oss-120b":
                    with open(Path(stage2_run_dir) / 'multiturn_alpha.jsonl', 'r') as f:
                        history_conversations = [json.loads(line) for line in f]
                    print(f"Mode: {mode}. Loaded history conversations from {Path(stage2_run_dir) / 'multiturn_alpha.jsonl'}")
                elif model_name == "azure/DeepSeek-V3.1":
                    with open(Path(stage2_run_dir) / 'multiturn_beta.jsonl', 'r') as f:
                        history_conversations = [json.loads(line) for line in f]
                    print(f"Mode: {mode}. Loaded history conversations from {Path(stage2_run_dir) / 'multiturn_beta.jsonl'}")
                else:
                    raise ValueError(f"Invalid model name: {model_name}")
            elif mode == "one-sided":
                with open(Path(stage2_run_dir) / 'multiturn_beta.jsonl', 'r') as f:
                    history_conversations = [json.loads(line) for line in f]
                print(f"Mode: {mode}. Loaded history conversations from {Path(stage2_run_dir) / 'multiturn_beta.jsonl'}")
            else:
                raise ValueError(f"Invalid mode: {mode}")
            
            history_conversation = history_conversations[query_index]
            conversations = history_conversation['conversations']
            message_history = conversations[:rounds_num*2+2]
        else:
            message_history = []
            
        data_config = dict(
            dataset_name=dataset_name,
            query_index=query_index,
            datapoint=datapoint,
        )
    else:
        raise ValueError(f"Invalid exp_name: {exp_name}")
    
    messages = message_history + [dict(role='user', content=input_text)]
    generation_manager = GenerationManager(
        run_dir=run_dir,
        print_to_stdout=True,
        overwrite=True,
        dry_run=False,
    )
    
    generation_manager.save_generation_config(dict(
        model_config=model.config,
        exp_config=exp_config,
        data_config=data_config,
    ))

    output_text = model.generate_with_messages(messages)
    belief_results = parse_eval_output(output_text)
    response = dict(
        messages=messages,
        output_text=output_text,
        belief_results=belief_results,
    )
    generation_manager.write_prediction(response)
    generation_manager.write_log(f'### messages ###')
    for m in messages:
        generation_manager.write_log(f'role: {m["role"]}')
        generation_manager.write_log(f'content:')
        generation_manager.write_log(f'{m["content"]}')
        generation_manager.write_log('---')
    generation_manager.write_log(f'### output_text ###\n{output_text}')
    generation_manager.write_log('---')
    generation_manager.write_log(f'### belief_results ###\n{belief_results}')
    generation_manager.write_log('---')

    generation_manager.save_json(belief_results, 'belief_results.json')
    time.sleep(1.0)
    print(f'Run finished: {run_dir}')


if __name__ == '__main__':
    fire.Fire(main)
