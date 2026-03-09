# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

import json
import re
import logging
import traceback
from datetime import datetime, date as date_type, timedelta
from decimal import Decimal

_logger = logging.getLogger(__name__)

class OdooJSONEncoder(json.JSONEncoder):
    """
    Custom JSON encoder that handles Odoo-specific data types.
    """
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        elif isinstance(obj, date_type):
            return obj.isoformat()
        elif isinstance(obj, timedelta):
            return str(obj)
        elif isinstance(obj, Decimal):
            return float(obj)
        elif hasattr(obj, '__iter__') and not isinstance(obj, (str, bytes, dict)):
            # Handle Odoo recordsets and other iterables
            return list(obj)
        return super().default(obj)

def clean_ai_response(ai_response):
    """
    Clean AI response by removing raw JSON/dictionary outputs.
    Safety net in case the AI ignores prompt instructions.
    """
    if not ai_response:
        return ai_response

    # Pattern to detect JSON-like structures at the start of response
    json_pattern = r'^\s*[\{\[][\"\']?\w+[\"\']?\s*:'

    if re.match(json_pattern, ai_response):
        _logger.warning(f"⚠️ Detected raw JSON in AI response, attempting to clean: {ai_response[:100]}...")

        greeting_match = re.search(r'(Hello|Hi|Dear)\s+\w+', ai_response, re.IGNORECASE)
        if greeting_match:
            cleaned = ai_response[greeting_match.start():]
            return cleaned.strip()

        return "I apologize, but I had trouble formatting that response. Could you please rephrase your question?"

    return ai_response

def get_friendly_error_message(technical_error):
    """Convert technical error messages to user-friendly messages"""
    error_str = str(technical_error).lower()

    try:
        if hasattr(technical_error, 'message'):
            error_str = technical_error.message.lower()
        elif hasattr(technical_error, 'args') and len(technical_error.args) > 0:
            error_str = str(technical_error.args[0]).lower()
    except Exception:
        pass

    error_mappings = {
        'overloaded': "🔄 The AI service is currently overloaded with requests. Please wait a moment and try again.",
        '503': "🔄 The AI service is temporarily unavailable. Please try again in a few moments.",
        '429': "⏱️ Too many requests. Please wait a moment before trying again.",
        '500': "⚠️ The AI service encountered an error. Please try again.",
        'api key': "🔑 Connection issue: Please check if the API key is configured correctly.",
        'quota': "📊 Usage limit reached. Please try again later or contact your administrator.",
        'rate limit': "⏱️ The AI service is receiving too many requests. Please wait a moment and try again.",
        'timeout': "⏳ Request timeout. Please try again with a simpler question.",
        'connection': "🌐 Unable to connect to the AI service. Please check your internet connection.",
        'authentication': "🔐 Authentication issue. Please contact your administrator.",
        'permission': "🚫 You don't have permission to access this information. Contact your administrator if this seems wrong.",
        'not found': "❓ Information not found. Could you try rephrasing your question?",
        'invalid': "❓ I didn't understand that request. Could you please rephrase?",
        'network': "🌐 Network issue detected. Please check your connection and try again.",
        'unavailable': "🔄 The AI service is temporarily unavailable. Please try again shortly.",
        'server': "⚠️ Server error. Please try again in a few moments.",
    }

    for pattern, friendly_msg in error_mappings.items():
        if pattern in error_str:
            return friendly_msg

    return "I encountered an issue while processing your request. Please try rephrasing your question or contact support if the problem persists."

def sanitize_for_json(data):
    """
    Ensure data is JSON serializable by encoding and decoding using OdooJSONEncoder.
    This converts datetime/date objects to strings, Decimals to floats, etc.
    """
    if data is None:
        return None
    try:
        return json.loads(json.dumps(data, cls=OdooJSONEncoder))
    except (TypeError, ValueError) as e:
        _logger.error(f"Failed to sanitize data for JSON: {e}")
        return str(data)  # Fallback to string representation
