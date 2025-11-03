import os
import time
import json
from pathlib import Path
import yaml
import fire
from rich import print
from src.core.models import Model
from src.core.utils import GenerationManager, parse_eval_output
from src.core.tools import TOOL_REGISTRY, NAMES
from src.core.agent_runtime import AgentRuntime


WORKING_DIR = os.getcwd()
MULTITURN_DATA_DIR = f'{WORKING_DIR}/data/multiturn'
MULTITURN_PROMPTS_PATH = f'{WORKING_DIR}/src/prompts/multiturn.yaml'
STUDY_PROMPTS_PATH = f'{WORKING_DIR}/src/prompts/study.yaml'
STUDY_TOPICS_PATH = f'{WORKING_DIR}/data/study/topics.yaml'
STUDY_TOOLS_PATH = f'{WORKING_DIR}/data/study/tools.yaml'


def run_judge(judge_messages, judge_model):
    judge_outputs = judge_model.generate_with_messages(judge_messages)
    judge_results = parse_eval_output(judge_outputs, mode="label")
    try:
        answer_idx = judge_outputs.find("The answer is:")
        judge_text = judge_outputs[:answer_idx].strip()
    except Exception:
        judge_text = judge_outputs
    return dict(judge_results=judge_results, judge_text=judge_text)


def main(exp_name, **kwargs):
    seed = kwargs.get('seed', 0)
    stage2_run_dir = kwargs.get('stage2_run_dir', 'none')
    run_dir = kwargs.get('run_dir', None)
    assert run_dir is not None, 'run_dir is required'

    exp_config = dict(exp_name=exp_name, seed=seed, run_dir=run_dir, stage2_run_dir=stage2_run_dir)
    
    model_name = kwargs.get('model_name', 'gpt-5')
    model = Model(model_name)
    judge_model = Model("azure/gpt-5-mini")

    if exp_name == 'study':
        with open(STUDY_TOOLS_PATH, 'r') as f:
            TOOLS = yaml.safe_load(f)
        with open(STUDY_PROMPTS_PATH, 'r') as f:
            PROMPTS = yaml.safe_load(f)
        with open(STUDY_TOPICS_PATH, 'r') as f:
            TOPICS = yaml.safe_load(f)
    
        survey_topic_index = kwargs.get('survey_topic_index', 0)
        survey_topic_name = TOPICS['survey'][survey_topic_index]['topic_name']
        support_statement = TOPICS['survey'][survey_topic_index]['support_statement']
        oppose_statement = TOPICS['survey'][survey_topic_index]['oppose_statement']

        tool_cls = TOOL_REGISTRY.get(survey_topic_index)
        tool_impl = tool_cls()
        
        agentic_prompt = TOOLS['survey'][survey_topic_index]['prompt']
        agentic_tools = TOOLS['survey'][survey_topic_index]['tools']

        data_config = dict(
            survey_topic_index=survey_topic_index,
            survey_topic_name=survey_topic_name,
            prompts=agentic_prompt,
            tools=agentic_tools,
            prompt_text=agentic_prompt,
        )
        
        if stage2_run_dir != 'none':
            with open(Path(stage2_run_dir) / 'message_history.json', 'r') as f:
                message_history = json.load(f)
        else:
            message_history = []
        messages = message_history + [dict(role="user", content=agentic_prompt)]
        
        runtime = AgentRuntime(tool_impl, terminal_names=NAMES)
        try:
            loop_out = runtime.fc_agentic_loop(
                model=model,
                base_messages=messages,
                agentic_tools=agentic_tools,
                max_steps=kwargs.get("max_steps", 8),
                allow_repeat=False
            )
        except Exception as e:
            print(f"[Warning] FC failed: {e}. Falling back to JSON loop.")
            tools_schema_json = json.dumps(agentic_tools, ensure_ascii=False, indent=2)
            loop_out = runtime.json_agentic_loop(
                model=model,
                base_messages=messages,
                tools_schema_json=tools_schema_json,
                max_steps=kwargs.get("max_steps", 8),
                allow_repeat=False
            )

        results = loop_out["results"]
        did_submit = loop_out["did_submit"]
        final_text = loop_out["final_text"]
        messages = loop_out["messages"]

        print("\n[TOOL RESULTS]")
        print(json.dumps(results, ensure_ascii=False, indent=2))
        if final_text:
            print("\n[FINAL MESSAGE]")
            print(final_text.strip())
        if did_submit:
            print("\n[DONE]")

        judge_prompt = PROMPTS['behavior_judgement'].format(
            scenario=agentic_prompt,
            response=final_text,
            support_statement=support_statement,
            oppose_statement=oppose_statement,
        )

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
        input_text = datapoint['behavior_eval']
        
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
        messages = message_history + [dict(role='user', content=input_text)]
        final_text = model.generate_with_messages(messages)
        query = input_text
        statements = datapoint['statements']
        support_statement = statements['support_statement']
        oppose_statement = statements['oppose_statement']
        
        judge_prompt = PROMPTS['moral_behavior'].format(
            moral_dilemma=query, 
            response=final_text,
            oppose_statement=oppose_statement,
            support_statement=support_statement,
        ) if dataset_name == "moral" else PROMPTS['safety_behavior'].format(
            query=query,
            response=final_text,
            oppose_statement=oppose_statement,
            support_statement=support_statement,
        )
    else:
        raise ValueError(f"Invalid exp_name: {exp_name}")

    judge_messages = [dict(role='user', content=judge_prompt)]
    judge_outputs = run_judge(judge_messages, judge_model)
    judge_results = judge_outputs['judge_results']
    judge_text = judge_outputs['judge_text']
    
    response = dict(
        messages=messages,
        output_text=final_text,
        behavior_results=judge_results,
        judge_text=judge_text,
    )
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
    generation_manager.write_prediction(response)
    generation_manager.write_log(f'### messages ###')
    for m in messages:
        role = m.get("role", "")
        generation_manager.write_log(f'role: {role}')
        generation_manager.write_log('content:')
        if "content" in m:
            generation_manager.write_log(f'{m["content"]}')
        elif "tool_calls" in m:
            generation_manager.write_log(json.dumps(m["tool_calls"], ensure_ascii=False))
        else:
            generation_manager.write_log(json.dumps(m, ensure_ascii=False))
        generation_manager.write_log('---')
    generation_manager.write_log(f'### Final text ###\n{final_text}')
    generation_manager.write_log('---')
    generation_manager.write_log(f'### behavior_results ###\n{judge_results}')
    generation_manager.write_log('---')

    generation_manager.save_json(judge_results, 'behavior_results.json')
    time.sleep(1.0)
    print(f'Run finished: {run_dir}')


if __name__ == '__main__':
    fire.Fire(main)
