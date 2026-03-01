# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from google import genai

class GeminiProvider:
    def __init__(self, api_key):
        self.client = genai.Client(api_key=api_key)

    def generate_content(self, model, contents, config=None):
        """Basic Gemini API request"""
        return self.client.models.generate_content(
            model=model,
            contents=contents,
            config=config
        )
