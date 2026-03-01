# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from openai import OpenAI

class OpenAIProvider:
    def __init__(self, api_key):
        self.client = OpenAI(api_key=api_key)

    def generate_content(self, model, messages, tools=None, temperature=0.2):
        """
        OpenAI API request with function calling support.

        Args:
            model: Model name (e.g., 'gpt-4o', 'gpt-4-turbo')
            messages: List of message dicts with 'role' and 'content'
            tools: Optional list of function declarations
            temperature: Temperature value (0.0 to 1.0)

        Returns:
            OpenAI response object
        """
        # temperature params is not a variable in gpt 5 models and its by default set to 1 by providers not from input params.
        # model is technical name sh_model_name which is actual model code. 
        if 'gpt-5' in model :
            params = {
                'model' : model , 
                'messages' : messages,
            }
        else : 
            params = {
                'model': model,
                'messages': messages,
                'temperature': temperature,
            }

        if tools:
            params['tools'] = tools
            params['tool_choice'] = 'auto'

        return self.client.chat.completions.create(**params)
