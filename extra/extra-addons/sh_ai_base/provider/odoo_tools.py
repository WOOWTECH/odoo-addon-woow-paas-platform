# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

import json
import logging
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)

def fuzzy_lookup_declaration():
    """
    Function declaration for smart name-to-ID resolution.
    AI should use this BEFORE querying relational fields with names.
    """
    return {
        "name": "fuzzy_lookup",
        "description": "Smart name-to-ID resolver for relational fields. Use this BEFORE querying when you need to filter by a Many2one or Many2many field using a name. It searches all text fields dynamically and strips category words.",
        "parameters": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "The Odoo model to search (e.g., 'hr.department', 'res.partner')",
                },
                "search_term": {
                    "type": "string",
                    "description": "The name/term to search for (e.g., 'Treasure Department')",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum candidates to return (default: 5)",
                }
            },
            "required": ["model", "search_term"],
            "additionalProperties": False,
        },
    }


def fuzzy_lookup(env, model, search_term, limit=5):
    """
    Fully dynamic name-to-ID resolver.
    Searches across ALL text-based fields dynamically.
    No hardcoded suffixes or field names.
    """
    try:
        try:
            Model = env[model]
        except KeyError:
            return {"success": False, "error": f"Model '{model}' not found", "results": []}

        # SECURITY CHECK: Verify user has read access
        try:
            Model.browse().check_access('read')
        except Exception:
            return {"success": False, "error": "Access denied", "results": []}

        # Step 1: Detect and strip category words dynamically using ir.model
        clean_term = search_term.strip()
        model_info = env['ir.model'].sudo().search([('model', '=', model)], limit=1)
        model_display_name = model_info.name if model_info else (Model._description or '')
        
        if model_display_name:
            display_words = model_display_name.lower().split()
            term_words_lower = clean_term.lower().split()
            term_words_original = clean_term.split()
            
            # Remove trailing words that match model display name
            while term_words_lower and display_words and term_words_lower[-1] == display_words[-1]:
                term_words_lower.pop()
                term_words_original.pop()
                display_words.pop()
            
            if term_words_original:
                clean_term = ' '.join(term_words_original)

        # Step 2: Dynamically identify text-searchable fields
        fields_info = Model.fields_get()
        searchable_fields = [
            fname for fname, info in fields_info.items()
            if info.get('type') in ('char', 'text', 'html') and info.get('searchable', True) and not fname.startswith('_')
        ]
        
        if not searchable_fields:
            searchable_fields = [Model._rec_name] if hasattr(Model, '_rec_name') else ['name']

        # Step 3: Search using Odoo's native OR logic
        def get_or_domain(term, operator='ilike'):
            domain = ['|'] * (len(searchable_fields) - 1)
            for f in searchable_fields:
                domain.append((f, operator, term))
            return domain

        # Strategy A: Exact-ish match (=ilike)
        results = Model.search(get_or_domain(clean_term, '=ilike'), limit=limit)
        
        # Strategy B: Fuzzy match (ilike)
        if not results:
            results = Model.search(get_or_domain(clean_term, 'ilike'), limit=limit)
            
        # Strategy C: Word-by-word
        if not results and ' ' in clean_term:
            for word in clean_term.split():
                if len(word) > 2:
                    results = Model.search(get_or_domain(word, 'ilike'), limit=limit)
                    if results: break

        return {
            "success": True,
            "results": [{"id": r.id, "display_name": r.display_name} for r in results],
            "model": model,
            "search_term": search_term,
            "cleaned_term": clean_term,
            "fields_searched": searchable_fields[:5]
        }
    except Exception as e:
        return {"success": False, "error": str(e), "results": []}


def _validate_and_fix_domain(env, model, domain):
    """
    Dynamically intercept and fix domains with string values on relational fields.
    Handles Many2one and Many2many using Odoo metadata.
    """
    if not domain or not isinstance(domain, list):
        return domain, []
    
    corrections = []
    try:
        Model = env[model]
        model_fields = Model.fields_get()
    except Exception:
        return domain, []
    
    fixed_domain = []
    
    for clause in domain:
        # Handle operators ('&', '|', '!')
        if not isinstance(clause, (list, tuple)) or len(clause) != 3:
            fixed_domain.append(clause)
            continue
        
        field_name, operator, value = clause
        field_info = model_fields.get(field_name, {})
        field_type = field_info.get('type', '')
        
        # intercept string values on Many2one / Many2many
        if field_type in ('many2one', 'many2many') and isinstance(value, str):
            relation_model = field_info.get('relation')
            if relation_model:
                lookup = fuzzy_lookup(env, relation_model, value, limit=5)
                resolved = lookup.get('results', [])
                
                if resolved:
                    if field_type == 'many2one':
                        # Fix to ID match
                        res_id = resolved[0]['id']
                        res_name = resolved[0]['display_name']
                        fixed_clause = [field_name, '=', res_id]
                        fixed_domain.append(fixed_clause)
                        corrections.append(f"Fixed {field_name}: '{value}' -> {res_name} (ID: {res_id})")
                        continue
                    elif field_type == 'many2many':
                        # Fix to 'in' IDs
                        res_ids = [r['id'] for r in resolved]
                        fixed_clause = [field_name, 'in', res_ids]
                        fixed_domain.append(fixed_clause)
                        corrections.append(f"Fixed {field_name}: '{value}' -> {len(res_ids)} candidates")
                        continue
                else:
                    corrections.append(f"Warning: Could not resolve '{value}' for {field_name}")

        fixed_domain.append(clause)
        
    return fixed_domain, corrections


def _sanitize_field_value(value, max_length=80):
    """
    Sanitize field values for AI consumption.

    Prevents:
    - Prompt injection through extremely long field values
    - Context overflow from large text fields

    Args:
        value: Field value to sanitize
        max_length: Maximum length for string values

    Returns:
        Sanitized value safe for AI prompts
    """
    if isinstance(value, str):
        # Truncate long strings to prevent prompt injection
        if len(value) > max_length:
            return value[:max_length-3] + "..."
    return value


def _limit_relational_fields(records_data, max_items=30):
    """
    Limit items in relational fields to prevent context flooding.

    Args:
        records_data: List of record dictionaries
        max_items: Maximum items to keep in list/array fields

    Returns:
        Modified records with limited relational fields
    """
    for record in records_data:
        for key, value in list(record.items()):
            # Limit array/list fields (o2m, m2m typically return lists)
            if isinstance(value, (list, tuple)) and len(value) > max_items:
                record[key] = {
                    '_truncated': True,
                    'count': len(value),
                    'items': value[:max_items],
                    'note': f'Showing first {max_items} of {len(value)} items'
                }
    return records_data


def get_search_records_declaration():
    """
    Function declaration for searching Odoo records.
    This is a generic tool that works with any Odoo model.
    """
    return {
        "name": "search_records",
        "strict": True,
        "description": "Search and retrieve records from any Odoo model (e.g., sale.order, res.partner). Use this to query database information like counting records, finding specific data, or retrieving field values.",
        "parameters": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "The technical name of the Odoo model to search (e.g., 'sale.order', 'res.partner')",
                },
                "domain": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "anyOf": [
                                    {"type": "string"},  # For operators like '|', '&', '!'
                                    {
                                        "type": "array",  # For conditions like ['field', 'operator', 'value']
                                        "items": {
                                            "anyOf": [
                                                {"type": "string"},
                                                {"type": "number"},
                                                {"type": "boolean"},
                                                {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}]}}
                                            ]
                                        }
                                    }
                                ]
                            }
                        },
                        {"type": "null"}
                    ],
                    "description": "Odoo domain filter. Format: [[field, operator, value], ...] or ['|', [...], [...]]. Pass null if no filter needed.",
                },
                "fields": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        {"type": "null"}
                    ],
                    "description": "List of field names to retrieve. Pass null to get default fields.",
                },
                "limit": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "description": "Maximum records to return. Pass null for default limit (10).",
                },
                "order": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Sort order (e.g., 'amount_total desc'). Pass null for default.",
                },
                "offset": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "description": "Pagination offset. Pass null for 0.",
                },
                "group_by": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Field name to group by for list view grouping. Pass null if not needed.",
                },
                "count_only": {
                    "anyOf": [{"type": "boolean"}, {"type": "null"}],
                    "description": "Set to true if you only need the count. Pass null/false if actual data needed.",
                }
            },
            "required": ["model", "domain", "fields", "limit", "order", "offset", "group_by", "count_only"],
            "additionalProperties": False,
        },
    }


def search_records(env, model, domain=None, fields=None, limit=None, order=None, offset=0, group_by=None, count_only=False):
    """
    Enhanced search_records with superior performance and security.

    Uses Odoo's search_read() for 50% better performance (1 query vs 2).
    Includes our security enhancements and error handling.

    Args:
        env: Odoo environment
        model: Model technical name
        domain: Search domain (Python list or JSON string)
        fields: Fields to retrieve
        limit: Maximum records to return
        order: Sort order (e.g., 'name asc', 'amount_total desc')
        offset: Number of records to skip (for pagination)
        group_by: Field name to group by (for list view grouping)
        count_only: Return only count (no records)

    Returns:
        dict with success, count, records, and optional group_by
    """
    try:
        # Parse domain - support both Python list and JSON string
        if domain is None:
            domain = []
        elif isinstance(domain, str):
            # Support JSON string domains for flexibility
            import json
            try:
                domain = json.loads(domain)
            except json.JSONDecodeError:
                return {
                    "success": False,
                    "error": "Invalid domain format. Use Python list or valid JSON string.",
                    "model": model,
                }

        # Validate domain structure
        if not isinstance(domain, list):
            return {
                "success": False,
                "error": "Domain must be a list of conditions.",
                "model": model,
            }

        # Get model object
        try:
            Model = env[model]
        except KeyError:
            return {
                "success": False,
                "error": f"Model '{model}' not found. Please verify the model name.",
                "model": model,
            }

        # DYNAMIC FIX: Intercept string searches on relational fields
        original_domain = domain
        domain, corrections = _validate_and_fix_domain(env, model, domain)
        if corrections:
            _logger.info(f"🛠️ AI Domain Auto-Fix: {', '.join(corrections)}")


        # SECURITY CHECK: Verify user has read access to this model
        # This prevents unauthorized access to models user doesn't have permission for
        # Use browse() to create empty recordset - this checks model-level permissions
        try:
            Model.browse().check_access('read')
        except AccessError:
            return {
                "success": False,
                "error": "Access denied. You don't have permission to view this data.",
                "model": model,
            }

        # Count-only mode (fast)
        if count_only:
            count = Model.search_count(domain)
            return {
                "success": True,
                "count": count,
                "model": model,
            }

        # If group_by specified, ensure it's in fields list
        if group_by and fields and group_by not in fields:
            fields = list(fields)  # Copy to avoid modifying original
            fields.append(group_by)

        # CONSISTENCY ENFORCEMENT: Default limit to 10 for better UX
        # This ensures AI shows manageable results and "View All" button appears
        if limit is None:
            limit = 10

        # CRITICAL: Maximum limit enforcement - NEVER allow more than 10 records in chat
        # This prevents UI overload and ensures consistent UX even with millions of records
        MAX_DISPLAY_LIMIT = 10
        if limit > MAX_DISPLAY_LIMIT:
            limit = MAX_DISPLAY_LIMIT

        # PERFORMANCE UPGRADE: Use search_read() instead of search() + read()
        # This reduces database queries from 2 to 1 (50% performance improvement)
        data = Model.search_read(
            domain=domain,
            fields=fields,
            offset=offset,
            limit=limit,
            order=order
        )

        # Get total count for pagination info
        total_count = Model.search_count(domain)

        # OUR SECURITY ENHANCEMENT: Sanitize field values
        for record in data:
            # Sanitize text fields to prevent prompt injection
            for field_name in ['name', 'display_name', 'description', 'note', 'notes']:
                if field_name in record:
                    record[field_name] = _sanitize_field_value(record[field_name])

        # OUR PERFORMANCE ENHANCEMENT: Limit relational fields
        data = _limit_relational_fields(data)

        # Build enhanced response
        response = {
            "success": True,
            "count": len(data),
            "total_count": total_count,  # Total matching records (for pagination)
            "model": model,
            "records": data,
            "domain": domain,
            "order": order,
            "offset": offset,
            "limit": limit,
        }

        # Include group_by for UI grouping
        if group_by:
            response["group_by"] = group_by

        return response

    except AccessError:
        # Odoo's AccessError is raised when user lacks permission
        return {
            "success": False,
            "error": "Access denied. You don't have permission to view this data.",
            "model": model,
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Error querying {model}: {str(e)}",
            "model": model,
        }


def get_models_list_declaration():
    """
    Function declaration for discovering available Odoo models.
    This tool enables AI to learn what models exist in the database.
    """
    return {
        "name": "get_models_list",
        "strict": True,
        "description": "Refresh the list of available Odoo models. Returns all models you have read access to.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    }


def get_models_list(env):
    """
    Execute the get_models_list function.
    Returns list of ALL models current user has READ access to with rich metadata.

    Security: Uses Odoo's built-in get_available_models() which:
    - Checks user is internal
    - Checks read access rights
    - Filters transient/abstract models
    - No sudo() - respects current user context

    Args:
        env: Odoo environment

    Returns:
        dict with list of accessible models with metadata
    """
    try:
        # Use Odoo's security-aware method from web module
        # This respects access rights and only returns models user can read
        all_models = env['ir.model'].get_available_models()

        # Enhance each model with additional context
        enhanced_models = []
        for model in all_models:
            enhanced = {
                "model": model['model'],
                "display_name": model['display_name'],
            }

            # Try to get model description if available
            try:
                Model = env[model['model']]
                if hasattr(Model, '_description') and Model._description:
                    enhanced["description"] = Model._description
            except Exception:
                pass

            enhanced_models.append(enhanced)

        return {
            "success": True,
            "count": len(enhanced_models),
            "models": enhanced_models,
            "note": "Complete catalog of models you have access to"
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


def get_model_fields_declaration():
    """
    Function declaration for discovering fields of Odoo models.
    """
    return {
        "name": "get_model_fields",
        "strict": True,
        "description": "Discover fields and structure of Odoo models. Use this to understand what fields exist, their types, and relationships.",
        "parameters": {
            "type": "object",
            "properties": {
                "models": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of model technical names to get fields for (e.g., ['sale.order']).",
                },
                "field_types": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        {"type": "null"}
                    ],
                    "description": "Optional: Filter by field types (e.g., ['many2one']). Pass null for all fields.",
                }
            },
            "required": ["models", "field_types"],
            "additionalProperties": False,
        },
    }


def get_model_fields(env, models, field_types=None):
    """
    Execute the get_model_fields function.
    Returns field information for specified models.

    Security: Uses current user's env, respects access rights.
    Only returns fields from models the user can access.

    Args:
        env: Odoo environment
        models: List of model names (or single string)
        field_types: Optional list of field types to filter

    Returns:
        dict with field information for each model
    """
    try:
        # Convert single model to list
        if isinstance(models, str):
            models = [models]

        result = {}

        for model_name in models:
            try:
                # Check if user has access to this model
                Model = env[model_name]
                # Use browse() to create empty recordset - this checks model-level permissions
                if not Model.browse().has_access('read'):
                    result[model_name] = {
                        'success': False,
                        'error': 'Access denied',
                    }
                    continue

                # Get fields using fields_get() which is security-aware
                fields_data = Model.fields_get()

                # Filter by field types if specified
                if field_types:
                    fields_data = {
                        fname: fdata
                        for fname, fdata in fields_data.items()
                        if fdata.get('type') in field_types
                    }

                # Simplify field data for AI consumption
                simplified_fields = {}
                for fname, fdata in fields_data.items():
                    simplified_fields[fname] = {
                        'type': fdata.get('type'),
                        'string': fdata.get('string'),  # Human-readable label
                        'required': fdata.get('required', False),
                        'readonly': fdata.get('readonly', False),
                        'relation': fdata.get('relation'),  # Related model for relational fields
                        'help': fdata.get('help', ''),
                    }
                    
                    # Add relational metadata for AI decision making
                    if fdata.get('type') in ('many2one', 'many2many', 'one2many'):
                        rel_model = fdata.get('relation')
                        try:
                            # Get related model's display name dynamically
                            rel_info = env['ir.model'].sudo().search([('model', '=', rel_model)], limit=1)
                            simplified_fields[fname]['relation_label'] = rel_info.name if rel_info else rel_model
                        except Exception:
                            simplified_fields[fname]['relation_label'] = rel_model

                result[model_name] = {
                    'success': True,
                    'field_count': len(simplified_fields),
                    'fields': simplified_fields,
                }

            except Exception as e:
                result[model_name] = {
                    'success': False,
                    'error': f"Cannot access model: {str(e)}",
                }

        return {
            "success": True,
            "models_count": len(models),
            "models": result,
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


def get_selection_values_declaration():
    """
    Function declaration for getting valid selection field values.
    """
    return {
        "name": "get_selection_values",
        "strict": True,
        "description": "Get valid values for selection fields (state, type, priority, etc.).",
        "parameters": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "The technical name of the Odoo model (e.g., 'sale.order')",
                },
                "field": {
                    "type": "string",
                    "description": "The selection field name (e.g., 'state')",
                },
            },
            "required": ["model", "field"],
            "additionalProperties": False,
        },
    }


def get_selection_values(env, model, field):
    """
    Get valid values for a selection field.

    Args:
        env: Odoo environment
        model: Model name
        field: Selection field name

    Returns:
        dict with selection values (value → label mapping)
    """
    try:
        # Check if model exists and user has access
        try:
            Model = env[model]
        except KeyError:
            return {
                "success": False,
                "error": f"Model '{model}' not found",
            }

        # Check read access
        # Use browse() to create empty recordset - this checks model-level permissions
        if not Model.browse().has_access('read'):
            return {
                "success": False,
                "error": f"Access denied for model '{model}'",
            }

        # Get field info
        fields_data = Model.fields_get([field])

        if field not in fields_data:
            return {
                "success": False,
                "error": f"Field '{field}' not found in model '{model}'",
            }

        field_info = fields_data[field]

        # Check if it's a selection field
        if field_info.get('type') != 'selection':
            return {
                "success": False,
                "error": f"Field '{field}' is not a selection field (type: {field_info.get('type')})",
            }

        # Get selection values
        selection_values = field_info.get('selection', [])

        if not selection_values:
            return {
                "success": True,
                "model": model,
                "field": field,
                "values": {},
                "note": "No selection values defined for this field"
            }

        # Convert to dict for easier consumption
        values_dict = {value: label for value, label in selection_values}

        return {
            "success": True,
            "model": model,
            "field": field,
            "field_label": field_info.get('string', field),
            "values": values_dict,
            "count": len(values_dict),
            "note": f"Use these values when filtering by '{field}' in domain filters"
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


def get_current_date_info_declaration():
    """
    Function declaration for getting current date and time information.
    """
    return {
        "name": "get_current_date_info",
        "strict": True,
        "description": "Get current date, time, and period information (this week, this month, etc.). Use this for queries with 'today', 'last month', etc.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    }


def get_current_date_info(env):
    """
    Get current date and time information with period calculations.

    Args:
        env: Odoo environment (not used but kept for consistency)

    Returns:
        dict with current date, time, and period information
    """
    try:
        from datetime import datetime, timedelta
        from dateutil.relativedelta import relativedelta

        now = datetime.now()

        # Calculate quarter
        current_quarter = (now.month - 1) // 3 + 1
        quarter_start_month = (current_quarter - 1) * 3 + 1
        quarter_start = datetime(now.year, quarter_start_month, 1)

        if current_quarter < 4:
            quarter_end_month = current_quarter * 3 + 1
            quarter_end = datetime(now.year, quarter_end_month, 1) - timedelta(days=1)
        else:
            quarter_end = datetime(now.year, 12, 31)

        # Calculate month boundaries
        month_start = datetime(now.year, now.month, 1)
        next_month = month_start + relativedelta(months=1)
        month_end = next_month - timedelta(days=1)

        # Calculate year boundaries
        year_start = datetime(now.year, 1, 1)
        year_end = datetime(now.year, 12, 31)

        # Calculate week boundaries
        week_start = now - timedelta(days=now.weekday())
        week_end = week_start + timedelta(days=6)

        return {
            "success": True,
            "current_datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "current_date": now.strftime("%Y-%m-%d"),
            "current_time": now.strftime("%H:%M:%S"),
            "year": now.year,
            "month": now.month,
            "day": now.day,
            "quarter": current_quarter,
            "periods": {
                "this_quarter": {
                    "start": quarter_start.strftime("%Y-%m-%d"),
                    "end": quarter_end.strftime("%Y-%m-%d"),
                    "quarter_number": current_quarter
                },
                "this_month": {
                    "start": month_start.strftime("%Y-%m-%d"),
                    "end": month_end.strftime("%Y-%m-%d"),
                    "month_number": now.month
                },
                "this_year": {
                    "start": year_start.strftime("%Y-%m-%d"),
                    "end": year_end.strftime("%Y-%m-%d"),
                    "year_number": now.year
                },
                "this_week": {
                    "start": week_start.strftime("%Y-%m-%d"),
                    "end": week_end.strftime("%Y-%m-%d")
                }
            }
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


def get_aggregate_records_declaration():
    """
    Advanced aggregation with ORM-powered database-side calculations.
    """
    return {
        "name": "aggregate_records",
        "strict": True,
        "description": "Perform aggregation (SUM, COUNT, AVG, MIN, MAX). Use for totals, averages, top X queries, etc. Database-side calculations.",
        "parameters": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "Technical model name (e.g., 'sale.order')",
                },
                "operation": {
                    "type": "string",
                    "enum": ["sum", "avg", "min", "max", "count"],
                    "description": "Aggregation: 'sum', 'avg', 'min', 'max', 'count'",
                },
                "domain": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "anyOf": [
                                    {"type": "string"},
                                    {
                                        "type": "array",
                                        "items": {
                                            "anyOf": [
                                                {"type": "string"},
                                                {"type": "number"},
                                                {"type": "boolean"},
                                                {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}]}}
                                            ]
                                        }
                                    }
                                ]
                            }
                        },
                        {"type": "null"}
                    ],
                    "description": "Filter domain. array of arrays. Pass null for all records.",
                },
                "field": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Field to aggregate. Required for sum/avg/min/max. Pass null for count.",
                },
                "group_by": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Group by field (e.g., 'partner_id'). Pass null if not needed.",
                },
                "having": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "type": "array",
                                "items": {
                                    "anyOf": [
                                        {"type": "string"},
                                        {"type": "number"}
                                    ]
                                }
                            }
                        },
                        {"type": "null"}
                    ],
                    "description": "Post-aggregation filter. Pass null if not needed.",
                },
                "limit": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "description": "Limit results (e.g., for 'top 10'). Pass null if not needed.",
                },
            },
            "required": ["model", "operation", "domain", "field", "group_by", "having", "limit"],
            "additionalProperties": False,
        },
    }


def aggregate_records(env, model, operation, domain=None, field=None, group_by=None, having=None, limit=None, **kwargs):
    """
    SUPERIOR aggregate_records using Odoo's _read_group() ORM method.

    Performance: Database-side aggregation (10x+ faster than Python loops)
    Features: Grouping, having clause, limit support
    Security: User permissions, field sanitization, error handling

    Our improvements over standard Odoo:
    - AI-friendly error messages (returned, not raised)
    - Field sanitization for security
    - JSON domain support
    - Simpler operation-based API
    - Better response format

    Args:
        env: Odoo environment
        model: Model name
        operation: 'sum', 'avg', 'min', 'max', 'count'
        domain: Filter domain (Python list or JSON string)
        field: Field to aggregate
        group_by: Group by field name
        having: Post-aggregation filter
        limit: Limit number of results (applied after sorting by result desc)

    Returns:
        dict with success, result, and optional groups
    """
    try:
        # Validate operation
        valid_operations = ['sum', 'avg', 'min', 'max', 'count']
        if operation not in valid_operations:
            return {
                "success": False,
                "error": f"Invalid operation '{operation}'. Must be one of: {', '.join(valid_operations)}",
            }

        # Validate model access
        try:
            Model = env[model]
        except KeyError:
            return {
                "success": False,
                "error": f"Model '{model}' not found. Please verify the model name.",
            }

        # Check read access (Odoo 19 uses check_access instead of check_access_rights)
        # Use browse() to create empty recordset - this checks model-level permissions
        try:
            Model.browse().check_access('read')
        except AccessError:
            return {
                "success": False,
                "error": "Access denied. You don't have permission to view this data.",
            }

        # Validate field requirement
        if operation in ['sum', 'avg', 'min', 'max'] and not field:
            return {
                "success": False,
                "error": f"Field parameter is required for '{operation}' operation.",
            }

        # Parse domain - support both Python list and JSON string
        if domain is None:
            domain = []
        elif isinstance(domain, str):
            try:
                domain = json.loads(domain)
            except json.JSONDecodeError:
                return {
                    "success": False,
                    "error": "Invalid domain format. Use Python list or valid JSON string.",
                }

        # Parse having clause if provided
        if having and isinstance(having, str):
            try:
                having = json.loads(having)
            except json.JSONDecodeError:
                return {
                    "success": False,
                    "error": "Invalid having format. Use Python list or valid JSON string.",
                }

        # DYNAMIC FIX: Intercept string searches on relational fields
        domain, corrections = _validate_and_fix_domain(env, model, domain)
        if corrections:
            _logger.info(f"🛠️ AI Aggregate Domain Auto-Fix: {', '.join(corrections)}")

        # GROUPED AGGREGATION using ORM _read_group()
        if group_by:
            try:
                # Build aggregation spec for _read_group()
                # Map our simple operations to Odoo's aggregate format
                if operation == 'count':
                    aggregates = [f'{group_by}:count_distinct']  # Count distinct groups
                else:
                    aggregates = [f'{field}:{operation}']

                # Use Odoo's powerful _read_group() ORM method
                # This executes aggregation on DATABASE side (super fast!)
                result_groups = Model._read_group(
                    domain=domain,
                    groupby=[group_by],
                    aggregates=aggregates,
                    having=having or [],
                    offset=0,
                    limit=None,  # We'll limit after sorting
                    order=None  # We'll sort in Python for simplicity
                )

                # Transform ORM results to our AI-friendly format
                # _read_group returns tuples: (groupby_value, aggregate_value)
                groups = []
                for group_tuple in result_groups:
                    # Odoo 19 _read_group returns tuples, not dicts
                    # Format: (groupby_value, aggregate_value)
                    group_key_value = group_tuple[0]  # First element is groupby value
                    agg_value = group_tuple[1] if len(group_tuple) > 1 else 0  # Second is aggregate

                    # Handle different field types for group key
                    if hasattr(group_key_value, '_name'):
                        # Many2one/relational field returns recordset
                        if group_key_value:
                            group_key = group_key_value.id
                            group_display = group_key_value.display_name
                        else:
                            group_key = False
                            group_display = "Not Set"
                    elif group_key_value is False or group_key_value is None:
                        group_key = False
                        group_display = "Not Set"
                    else:
                        # Selection, char, integer fields
                        group_key = group_key_value
                        group_display = str(group_key_value)

                    groups.append({
                        'group_key': group_key,
                        'group_display': group_display,
                        'result': agg_value or 0,
                    })

                # Sort by aggregated value (highest first) for "top X" queries
                groups.sort(key=lambda x: x['result'], reverse=True)

                # Apply limit after sorting
                if limit:
                    groups = groups[:limit]

                return {
                    "success": True,
                    "operation": operation,
                    "model": model,
                    "field": field,
                    "group_by": group_by,
                    "grouped": True,
                    "groups": groups,
                    "total_groups": len(groups),
                    "having_applied": bool(having),
                    "limit_applied": bool(limit)
                }

            except Exception as e:
                return {
                    "success": False,
                    "error": f"Grouped aggregation failed: {str(e)}",
                }

        # SIMPLE AGGREGATION (no grouping)
        # Still use ORM for consistency and performance
        try:
            if operation == 'count':
                # Fast count
                result = Model.search_count(domain)
                return {
                    "success": True,
                    "operation": operation,
                    "model": model,
                    "result": result,
                    "count": result,
                    "grouped": False
                }

            # For other operations, use _read_group without grouping
            # This is faster than search + Python aggregation
            aggregates = [f'{field}:{operation}']
            result_data = Model._read_group(
                domain=domain,
                groupby=[],
                aggregates=aggregates,
                having=[],
                offset=0,
                limit=None,
                order=None
            )

            # _read_group returns list of tuples
            # With no groupby, returns single tuple with just the aggregate value
            if result_data and len(result_data) > 0:
                result = result_data[0][0] if len(result_data[0]) > 0 else 0
            else:
                result = 0

            return {
                "success": True,
                "operation": operation,
                "model": model,
                "field": field,
                "result": result or 0,
                "grouped": False
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Aggregation failed: {str(e)}",
            }

    except Exception as e:
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}",
        }


def get_open_view_declaration():
    """
    Function declaration for opening Odoo views directly.
    """
    return {
        "name": "open_view",
        "strict": True,
        "description": "Open a specific Odoo view (list, kanban, graph, pivot) for a model. Use this for navigation requests.",
        "parameters": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "Technical model name (e.g., 'sale.order')",
                },
                "view_type": {
                    "type": "string",
                    "enum": ["list", "kanban", "graph", "pivot"],
                    "description": "View type to open.",
                },
                "domain": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "anyOf": [
                                    {"type": "string"},
                                    {
                                        "type": "array",
                                        "items": {
                                            "anyOf": [
                                                {"type": "string"},
                                                {"type": "number"},
                                                {"type": "boolean"},
                                                {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}]}}
                                            ]
                                        }
                                    }
                                ]
                            }
                        },
                        {"type": "null"}
                    ],
                    "description": "Optional filter domain. array of arrays. Pass null if all records.",
                },
                "group_by": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Optional grouping field. Pass null if not needed.",
                },
                "graph_mode": {
                    "anyOf": [
                        {"type": "string", "enum": ["bar", "line", "pie"]},
                        {"type": "null"}
                    ],
                    "description": "For graph views: bar, line, or pie. Pass null for default.",
                },
            },
            "required": ["model", "view_type", "domain", "group_by", "graph_mode"],
            "additionalProperties": False,
        },
    }


def open_view(env, model, view_type, domain=None, group_by=None, graph_mode=None):
    """
    Open a specific Odoo view for a model.

    This tool creates an action that opens the specified view directly.
    Used when user explicitly requests to navigate to a view.

    Args:
        env: Odoo environment
        model: Model technical name
        view_type: View type (list, kanban, graph, pivot)
        domain: Optional filter domain
        group_by: Optional grouping field
        graph_mode: Optional chart type for graph views (bar, line, pie)

    Returns:
        dict with action_data for frontend to open the view
    """
    try:
        # Validate model access
        try:
            Model = env[model]
        except KeyError:
            return {
                "success": False,
                "error": f"Model '{model}' not found. Please verify the model name.",
            }

        # SECURITY CHECK: Verify user has read access to this model
        # This prevents unauthorized access to models user doesn't have permission for
        # Use browse() to create empty recordset - this checks model-level permissions
        try:
            Model.browse().check_access('read')
        except AccessError:
            return {
                "success": False,
                "error": "Access denied. You don't have permission to view this data.",
            }

        # Validate view_type
        allowed_views = ['list', 'kanban', 'graph', 'pivot']
        if view_type not in allowed_views:
            return {
                "success": False,
                "error": f"Invalid view type '{view_type}'. Allowed: {', '.join(allowed_views)}",
            }

        # Validate graph_mode if provided
        if graph_mode:
            allowed_graph_modes = ['bar', 'line', 'pie']
            if graph_mode not in allowed_graph_modes:
                return {
                    "success": False,
                    "error": f"Invalid graph_mode '{graph_mode}'. Allowed: {', '.join(allowed_graph_modes)}",
                }

        # Parse domain
        if domain is None:
            domain = []
        elif isinstance(domain, str):
            import json
            try:
                domain = json.loads(domain)
            except json.JSONDecodeError:
                return {
                    "success": False,
                    "error": "Invalid domain format. Use Python list or valid JSON string.",
                }

        # DYNAMIC FIX: Intercept string searches on relational fields
        domain, corrections = _validate_and_fix_domain(env, model, domain)
        if corrections:
            _logger.info(f"🛠️ AI View Domain Auto-Fix: {', '.join(corrections)}")

        # Get total count for display
        total_count = Model.search_count(domain)

        # Get model display name
        model_display_name = Model._description or model

        # Return action data that will be used to open the view
        action_data = {
            "model": model,
            "view_type": view_type,
            "domain": domain,
            "total_count": total_count,
        }

        # Add group_by if specified
        if group_by:
            action_data["group_by"] = group_by

        # Add graph_mode if specified and view is graph
        if graph_mode and view_type == 'graph':
            action_data["graph_mode"] = graph_mode

        return {
            "success": True,
            "action_data": action_data,
            "message": f"Opening {model_display_name} in {view_type} view ({total_count} records).",
            "model": model,
            "view_type": view_type,
            "total_count": total_count,
        }

    except AccessError:
        # Odoo's AccessError is raised when user lacks permission
        return {
            "success": False,
            "error": "Access denied. You don't have permission to view this data.",
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Error opening view: {str(e)}",
        }