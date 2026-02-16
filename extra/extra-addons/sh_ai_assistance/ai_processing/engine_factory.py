# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from .gemini_engine import GeminiEngine
from .openai_engine import OpenAiEngine

class AiEngineFactory:
    """
    Factory to create the appropriate AI Engine based on provider type.
    """
    
    @staticmethod
    def get_engine(env, provider_type):
        if provider_type == 'openai':
            return OpenAiEngine(env)
        # Default to Gemini
        return GeminiEngine(env)
