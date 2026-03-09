#!/usr/bin/python3
# @Time    : 2022-08-10
# @Author  : Kevin Kong (kfx2007@163.com)

from odoo.tools import view_validation
from odoo.tools.view_validation import relaxng as rx
from odoo import tools
from lxml import etree
import os
import logging
import phonenumbers
from  phonenumbers import normalize_digits_only  

_logger = logging.getLogger(__name__)

def chunkify_list(items, chunk_size):
    for i in range(0, len(items), chunk_size):
        yield items[i:i+chunk_size]
        
def plain_number(number, country="CN"):
    national_format = phonenumbers.format_number(
        phonenumbers.parse(number, "CN"),
        phonenumbers.PhoneNumberFormat.NATIONAL
    )
    plain_number = normalize_digits_only(national_format)
    return plain_number

def fetch_binary(url):
    """
    Fetch binary data from a URL
    
    :param url: str, the URL to fetch the binary data from
    :return: bytes, the binary data
    """
    import requests
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.content
    except requests.RequestException as e:
        _logger.error("[Mommy Base] Error fetching binary from URL %s: %s", url, str(e))
        return False
    
def fetch_base64_binary(url):
    """
    Fetch base64 encoded binary data from a URL
    
    :param url: str, the URL to fetch the binary data from
    :return: str, base64 encoded binary data
    """
    import base64
    binary_data = fetch_binary(url)
    if binary_data:
        return base64.b64encode(binary_data)
    return False


_relaxng_cache = {}
def relaxng(view_type):
    try:
        with tools.file_open(os.path.join('mommy_base', 'rng', '%s_view.rng' % view_type)) as frng:
            try:
                relaxng_doc = etree.parse(frng)
                _relaxng_cache[view_type] = etree.RelaxNG(relaxng_doc)
            except Exception:
                # _logger.exception("[Mommy Base] Exception Relaxng")
                return rx(view_type)
            return _relaxng_cache[view_type]
    except FileNotFoundError:
        return rx(view_type)

view_validation.relaxng = relaxng