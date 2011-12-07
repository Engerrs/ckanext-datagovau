import logging
from ckan.lib.base import BaseController, render, c, model, abort, request
from ckan.lib.base import redirect, _, config, h
import ckan.logic.action.create as create
import ckan.logic.action.update as update
import ckan.logic.action.get as get
from ckan.logic.converters import date_to_db, date_to_form, convert_to_extras, convert_from_extras
from ckan.lib.navl.dictization_functions import DataError, flatten_dict, unflatten
from ckan.logic import NotFound, NotAuthorized, ValidationError
from ckan.logic import tuplize_dict, clean_dict, parse_params
from ckan.logic.schema import package_form_schema
from ckan.plugins import IDatasetForm
from ckan.plugins import implements, SingletonPlugin
from ckan.lib.package_saver import PackageSaver
from ckan.lib.field_types import DateType, DateConvertError
from ckan.authz import Authorizer
from ckan.lib.navl.dictization_functions import Invalid
from ckanext.dgu.forms.package_gov_fields import GeoCoverageType
from ckan.lib.navl.dictization_functions import validate, missing
import ckan.logic.validators as val
import ckan.logic.schema as default_schema
from ckan.lib.navl.validators import (ignore_missing,
                                      not_empty,
                                      empty,
                                      ignore,
                                      keep_extras,
                                     )

log = logging.getLogger(__name__)

geographic_granularity = [('', ''),
                          ('national', 'national'),
                          ('regional', 'regional'),
                          ('local authority', 'local authority'),
                          ('ward', 'ward'),
                          ('point', 'point'),
                          ('other', 'other - please specify')]

update_frequency = [('', ''),
                    ('never', 'never'),
                    ('discontinued', 'discontinued'),
                    ('annual', 'annual'),
                    ('quarterly', 'quarterly'),
                    ('monthly', 'monthly'),
                    ('other', 'other - please specify')]

temporal_granularity = [("",""),
                       ("year","year"),
                       ("quarter","quarter"),
                       ("month","month"),
                       ("week","week"),
                       ("day","day"),
                       ("hour","hour"),
                       ("point","point"),
                       ("other","other - please specify")]


class ExamplePackageController(SingletonPlugin):

    implements(IDatasetForm, inherit=True)

    def package_form(self):
        return 'controller/package_plugin.html'

    def is_fallback(self):
        """
        Returns true iff this provides the fallback behaviour, when no other
        plugin instance matches a package's type.

        As this is not the fallback controller we should return False.  If 
        we were wanting to act as the fallback, we'd return True
        """
        return False

    def package_types(self):
        """
        Returns an iterable of package type strings.

        If a request involving a package of one of those types is made, then
        this plugin instance will be delegated to.

        There must only be one plugin registered to each package type.  Any
        attempts to register more than one plugin instance to a given package
        type will raise an exception at startup.
        """
        return ["example"]

    def _setup_template_variables(self, context, data_dict=None):
        c.licences = [('', '')] + model.Package.get_license_options()
        c.geographic_granularity = geographic_granularity
        c.update_frequency = update_frequency
        c.temporal_granularity = temporal_granularity 

        c.publishers = self.get_publishers()

        c.is_sysadmin = Authorizer().is_sysadmin(c.user)
        c.resource_columns = model.Resource.get_columns()

        ## This is messy as auths take domain object not data_dict
        pkg = context.get('package') or c.pkg
        if pkg:
            c.auth_for_change_state = Authorizer().am_authorized(
                c, model.Action.CHANGE_STATE, pkg)

    def _form_to_db_schema(self):

        schema = {
            'title': [not_empty, unicode],
            'name': [not_empty, unicode, val.name_validator, val.package_name_validator],
            'notes': [not_empty, unicode],

            'date_released': [date_to_db, convert_to_extras],
            'date_updated': [date_to_db, convert_to_extras],
            'date_update_future': [date_to_db, convert_to_extras],
            'update_frequency': [use_other, unicode, convert_to_extras],
            'update_frequency-other': [],
            'precision': [unicode, convert_to_extras],
            'geographic_granularity': [use_other, unicode, convert_to_extras],
            'geographic_granularity-other': [],
            'geographic_coverage': [ignore_missing, convert_geographic_to_db, convert_to_extras],
            'temporal_granularity': [use_other, unicode, convert_to_extras],
            'temporal_granularity-other': [],
            'temporal_coverage-from': [date_to_db, convert_to_extras],
            'temporal_coverage-to': [date_to_db, convert_to_extras],
            'url': [unicode],
            'taxonomy_url': [unicode, convert_to_extras],

            'resources': default_schema.default_resource_schema(),
            
            'published_by': [not_empty, unicode, convert_to_extras],
            'published_via': [ignore_missing, unicode, convert_to_extras],
            'author': [ignore_missing, unicode],
            'author_email': [ignore_missing, unicode],
            'mandate': [ignore_missing, unicode, convert_to_extras],
            'license_id': [ignore_missing, unicode],
            'tag_string': [ignore_missing, val.tag_string_convert],
            'national_statistic': [ignore_missing, convert_to_extras],
            'state': [val.ignore_not_admin, ignore_missing],

            'log_message': [unicode, val.no_http],

            '__extras': [ignore],
            '__junk': [empty],
        }
        return schema
    
    def _db_to_form_schema(data):
        schema = {
            'date_released': [convert_from_extras, ignore_missing, date_to_form],
            'date_updated': [convert_from_extras, ignore_missing, date_to_form],
            'date_update_future': [convert_from_extras, ignore_missing, date_to_form],
            'update_frequency': [convert_from_extras, ignore_missing, extract_other(update_frequency)],
            'precision': [convert_from_extras, ignore_missing],
            'geographic_granularity': [convert_from_extras, ignore_missing, extract_other(geographic_granularity)],
            'geographic_coverage': [convert_from_extras, ignore_missing, convert_geographic_to_form],
            'temporal_granularity': [convert_from_extras, ignore_missing, extract_other(temporal_granularity)],
            'temporal_coverage-from': [convert_from_extras, ignore_missing, date_to_form],
            'temporal_coverage-to': [convert_from_extras, ignore_missing, date_to_form],
            'taxonomy_url': [convert_from_extras, ignore_missing],

            'resources': default_schema.default_resource_schema(),
            'extras': {
                'key': [],
                'value': [],
                '__extras': [keep_extras]
            },
            'tags': {
                '__extras': [keep_extras]
            },
            
            'published_by': [convert_from_extras, ignore_missing],
            'published_via': [convert_from_extras, ignore_missing],
            'mandate': [convert_from_extras, ignore_missing],
            'national_statistic': [convert_from_extras, ignore_missing],
            '__extras': [keep_extras],
            '__junk': [ignore],
        }
        return schema

    def _check_data_dict(self, data_dict):
        return

    def get_publishers(self):
        return [('pub1', 'pub2')]


def use_other(key, data, errors, context):

    other_key = key[-1] + '-other'
    other_value = data.get((other_key,), '').strip()
    if other_value:
        data[key] = other_value

def extract_other(option_list):

    def other(key, data, errors, context):
        value = data[key]
        if value in dict(option_list).keys():
            return
        elif value is missing:
            data[key] = ''
            return
        else:
            data[key] = 'other'
            other_key = key[-1] + '-other'
            data[(other_key,)] = value
    return other
            
def convert_geographic_to_db(value, context):

    if isinstance(value, list):
        regions = value
    elif value:
        regions = [value]
    else:
        regions = []
        
    return GeoCoverageType.get_instance().form_to_db(regions)

def convert_geographic_to_form(value, context):

    return GeoCoverageType.get_instance().db_to_form(value)
