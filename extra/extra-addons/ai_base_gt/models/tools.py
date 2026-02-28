import csv
import inspect
import io
import logging
import threading
import docstring_parser
from functools import wraps
from pydantic import Field, create_model
from typing import Optional, Any, Callable, get_type_hints, TypeVar, cast
from odoo import api
from odoo.modules.registry import Registry
from odoo.sql_db import BaseCursor

_logger = logging.getLogger(__name__)

F = TypeVar('F', bound=Callable[..., Any])


def dict_list_to_csv(data_list: list[dict[str, Any]]) -> str:
    """Convert a list of dictionaries to CSV format string.
    
    Args:
        data_list: List of dictionaries with same keys
        
    Returns:
        CSV formatted string
    """
    if not data_list:
        return ""
    
    output = io.StringIO()
    fieldnames = list(data_list[0].keys())
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(data_list)
    return output.getvalue().strip()


def parse_docstring(func: Callable) -> dict[str, Any]:
    """
    Extracts the main description, parameter descriptions, and return type description from a function's docstring.

    :param func: The function from which to parse the docstring.
    :type func: Callable

    :return: A dictionary containing the summary, parameter descriptions, and return description.
    :rtype: dict[str, Any]
    """

    doc = docstring_parser.parse(func.__doc__ or "")
    summary = doc.short_description or "No description provided."
    if doc.long_description:
        summary += "\n\n" + doc.long_description
    param_descriptions = {p.arg_name: p.description for p in doc.params}
    return_description = doc.returns.description if doc.returns else "No return description."
    return {
        "summary": summary.strip() if summary else "",
        "params": param_descriptions,
        "return": return_description.strip() if return_description else "",
    }


def ai_tool(
    condition: Optional[Callable] = None,
    params_aliases: Optional[dict[str, list[str]]] = None
) -> Callable:
    """
    Decorator to mark a function as an AI tool.

    :param condition: A function which receives the current AI thread as parameter.
        The result of this function decides if the tool would be
        included in the current thread or not.
        Example:
            condition=lambda thread: thread.assistant_id.has_model_read_access
        This means that the tool would be included in the current thread if the assistant has model read access.
        Optional.
    :type condition: Callable

    :param params_aliases: Sometimes the AI is stupid and uses the wrong parameter name.
        This dictionary allows to map the correct parameter name with their accepted aliases.
        Example:
            params_aliases={
                'vals': ['values'],
                'res_ids': ['record_ids']
            }
        This means that the parameter 'vals' can be called with the alias 'values'.
        The parameter 'res_ids' can be called with the alias 'record_ids'.
        Optional.
    :type params_aliases: dict[str, list[str]]

    :return: The decorated function.
    :rtype: Callable
    """
    def decorator(func: Callable) -> Callable:

        @wraps(func)
        def wrapped(self, *args, **kwargs):
            if params_aliases:
                for param, aliases in params_aliases.items():
                    if param not in kwargs:
                        for alias in aliases:
                            if alias in kwargs:
                                kwargs[param] = kwargs.pop(alias)
                                break
            return func(self, *args, **kwargs)

        setattr(wrapped, "ai_tool", True)
        setattr(wrapped, "ai_condition", condition)
        setattr(wrapped, "ai_params_aliases", params_aliases)
        return wrapped

    return decorator


def ai_spec(func: Callable) -> dict:
    """
    Generate OpenAPI spec from a function with descriptions taken from the docstring.

    :param func: The function to process.
    :type func: Callable

    :return: OpenAPI spec for the function.
    :rtype: dict
    """
    type_hints = get_type_hints(func)
    signature = inspect.signature(func)
    doc_info = parse_docstring(func)
    param_docs = doc_info["params"]
    parameters = list(signature.parameters.keys())

    fields = {}
    for index, param_name in enumerate(parameters):
        if index == 0 and (param_name == "self" or param_name == "cls"):
            continue
        param = signature.parameters[param_name]
        param_type = type_hints.get(param_name, str)
        param_desc = param_docs.get(param_name, "No description provided.")
        default = ... if param.default == inspect.Parameter.empty else param.default
        # Use Field to attach description
        fields[param_name] = (param_type, Field(default, description=param_desc))

    # Dynamically create a Pydantic model for the parameters
    ParamModel = create_model(f"{func.__name__}_params", **fields)
    param_schema = ParamModel.model_json_schema()
    param_schema.pop('title')

    ai_spec = {
        "name": func.__name__,
        "description": doc_info["summary"],
        "parameters": param_schema,
    }
    return ai_spec


def after_commit(_func: Optional[F] = None, *, wait: bool = False) -> Callable[[F], F]:
    """Decorator to execute the wrapped method *after* the current
    PostgreSQL transaction is **committed**, in a dedicated Python
    *thread*.

    Parameters
    ----------
    wait : bool, default ``False``
        * ``True``  - run in a background thread **and** ``join`` the thread,
          blocking the caller until the job is finished.
        * ``False`` - fire-and-forget: start the background thread and return
          immediately (the default behaviour).

    Usage examples
    --------------
    >>> @after_commit  # same as @after_commit(wait=False)
    ... def _update_solr(self):
    ...     pass

    >>> @after_commit(wait=True)
    ... def _heavy_export(self):
    ...     pass
    """

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapped(self, *args, **kwargs):
            assert isinstance(self.env.cr, BaseCursor)
            dbname = self.env.cr.dbname
            context = self.env.context
            uid = self.env.uid
            su = self.env.su

            def _job():
                db_registry = Registry(dbname)
                try:
                    with db_registry.cursor() as cr:
                        env = api.Environment(cr, uid, context, su=su)
                        func(self.with_env(env), *args, **kwargs)
                except Exception as e:
                    _logger.warning("Error running %s after commit for record %s", func.__name__, self)
                    _logger.exception(e)

            @self.env.cr.postcommit.add
            def _execute_after_commit():
                thread = threading.Thread(target=_job, name=f"{func.__name__}_postcommit_thread", daemon=True)
                thread.start()
                if wait:
                    thread.join()

        return cast(F, wrapped)

    # Support both @after_commit and @after_commit(wait=True) syntaxes
    if _func is None:
        return decorator
    return decorator(_func)
