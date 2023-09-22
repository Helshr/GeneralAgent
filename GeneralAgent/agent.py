# Agent

import logging
logging.basicConfig(level=logging.INFO)
import os, re
from jinja2 import Template
from collections import OrderedDict
from GeneralAgent.prompts import general_agent_prompt
from GeneralAgent.llm import llm_inference
from GeneralAgent.memory import Memory, MemoryNode
from GeneralAgent.interpreter import CodeInterpreter
from GeneralAgent.tools import Tools, scrape_web, llm


class Agent:
    def __init__(self, workspace, tools=None, max_plan_depth=4):
        self.max_plan_depth = max_plan_depth
        self.workspace = workspace
        if not os.path.exists(workspace):
            os.makedirs(workspace)
        self.memory = Memory(f'{workspace}/memory.json')
        self.interpreter = CodeInterpreter(f'{workspace}/code.bin')
        self.tools = tools or Tools([])

    def run(self, input=None, for_node_id=None, output_recall=None):
        input_node = self.insert_node(input, for_node_id) if input is not None else None
        todo_node = self.memory.get_todo_node() or input_node
        while todo_node is not None:
            if len(todo_node.childrens) > 0 and self.memory.is_all_children_success(todo_node):
                self.memory.success_node(todo_node)
            else:
                result, new_node, is_stop = self.execute_node(todo_node)
                if todo_node.action == 'plan':
                    result = f'[{todo_node.content}]\n{result}'
                output_recall(result)
                if is_stop:
                    return new_node.node_id        
            todo_node = self.memory.get_todo_node()
        return None

    def insert_node(self, input, for_node_id=None):
        node = MemoryNode(role='user', action='input', content=input)
        if for_node_id is None:
            self.memory.add_node(node)
        else:
            for_node = self.memory.get_node(for_node_id)
            self.memory.add_node_after(for_node, node)
            self.memory.success_node(for_node)
            self.memory.success_node(node) 
        return node
    
    def execute_node(self, node):
        # python_libs = ', '.join([line.strip() for line in open(os.path.join(os.path.dirname(__file__), 'requirements.txt'), 'r').readlines()])
        python_libs = ''
        python_funcs = self.tools.get_funs_description()
        system_variables = {
            'python_libs': python_libs,
            'python_funcs': python_funcs
        }
        system_prompt = Template(general_agent_prompt).render(**system_variables)
        messages = [{'role': 'system', 'content': system_prompt}] + self.memory.get_related_messages_for_node(node)
        # TODO: when messages exceed limit
        llm_response = llm_inference(messages)

        # answer node
        answer_node = MemoryNode(role='system', action='answer', content=llm_response)
        self.memory.add_node_after(node, answer_node)
        
        # process: code -> variable -> plan -> ask
        result = self.run_code_in_text(llm_response)
        result = self.replace_variable_in_text(result)
        has_plan, result = self.extract_plan_in_text(answer_node, result)
        has_ask, result = check_has_ask(result)
        result = result.replace('\n\n', '\n').strip()

        self.memory.success_node(node)
        if not has_plan:
            self.memory.success_node(answer_node)

        is_stop = has_ask
        
        return result, answer_node, is_stop
        
    def run_code_in_text(self, string):
        pattern = re.compile(r'```python\n(.*?)\n```', re.DOTALL)
        matches = pattern.findall(string)
        for code in matches:
            success, sys_out = self.interpreter.run_code(code)
            # TODO: when success is False, do something
            string = string.replace('```python\n{}\n```'.format(code), sys_out)
        return string
    
    def replace_variable_in_text(self, string):
        pattern = re.compile(r'#\$(.*?)\$#', re.DOTALL)
        matches = pattern.findall(string)
        for match in matches:
            value = self.interpreter.get_variable(match)
            if value is not None:
                string = string.replace('#${}$#'.format(match), str(value))
        return string

    def extract_plan_in_text(self, current_node, string):
        has_plan = False
        pattern = re.compile(r'```plan(.*?)```', re.DOTALL)
        matches = pattern.findall(string)
        for match in matches:
            has_plan = True
            plan_dict = structure_plan(match.strip())
            self.add_plans_for_node(current_node, plan_dict)
            string = string.replace('```plan{}```'.format(match), match)
        return has_plan, string
    
    def add_plans_for_node(self, node:MemoryNode, plan_dict):
        if self.memory.get_node_level(node) >= self.max_plan_depth:
            return
        for k, v in plan_dict.items():
            new_node = MemoryNode(role='system', action='plan', content=k.strip())
            self.memory.add_node_in(node, new_node)
            if len(v) > 0:
                self.add_plans_for_node(new_node, v)


def check_has_ask(string):
    pattern = re.compile(r'###ask', re.DOTALL)
    has_ask = pattern.search(string) is not None
    string = pattern.sub('', string)
    return has_ask, string


def structure_plan(data):
    structured_data = OrderedDict()
    current_section = [structured_data]
    for line in data.split('\n'):
        if not line.strip():
            continue
        depth = line.count('    ')
        section = line.strip()
        while depth < len(current_section) - 1:
            current_section.pop()
        current_section[-1][section] = OrderedDict()
        current_section.append(current_section[-1][section])
    return structured_data