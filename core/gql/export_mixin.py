import json
import logging
import re
import types
from typing import Dict, List, Callable

import graphene
from django.core.exceptions import FieldDoesNotExist
from django.db import models
from graphene.types.generic import GenericScalar
from graphene_django import DjangoObjectType
from pandas import DataFrame

from core.custom_filters import CustomFilterWizardStorage
from core.models import ExportableQueryModel, User


logger = logging.getLogger(__file__)

_JSON_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ExportableQueryMixin:
    export_patches: Dict[str, List[Callable[[DataFrame], DataFrame]]] = {}
    module_name: str
    object_type: str
    related_field: str

    @classmethod
    def get_module_name(cls) -> str:
        if not hasattr(cls, "module_name") or getattr(cls, "module_name") is None:
            error_msg = (
                "The class using `ExportableQueryMixin` must specify the `module_name` property "
                "when the customFilters argument is specified. Please override the `module_name` "
                "property in the Query schema to specify how to append custom filters for the specific object."
            )
            raise NotImplementedError(error_msg)
        return cls.module_name

    @classmethod
    def get_object_type(cls) -> str:
        if not hasattr(cls, "object_type") or getattr(cls, "object_type") is None:
            error_msg = (
                "The class using `ExportableQueryMixin` must specify the `object_type` property "
                "when the customFilters argument is specified. Please override the `object_type` "
                "property in the Query schema to specify how to append custom filters for the specific object."
            )
            raise NotImplementedError(error_msg)
        return cls.object_type

    @classmethod
    def get_related_field(cls):
        if not hasattr(cls, "related_field") or getattr(cls, "related_field") is None:
            return None
        return cls.related_field

    @classmethod
    def get_exportable_fields(cls):
        if not hasattr(cls, "exportable_fields"):
            raise NotImplementedError(
                "Class using `ExportableQueryMixin` has to provide either exportable_fields "
                "or overwrite`get_exportable_fields` to provide list of fields that can be exported."
            )
        return cls.exportable_fields

    @classmethod
    def get_patches_for_field(cls, field_type):
        if field_type not in cls.get_exportable_fields():
            raise AttributeError(f"Field {field_type} is not being exported.")
        return cls.export_patches.get(field_type)

    @classmethod
    def __init_subclass_with_meta__(cls, **meta_options):
        cls._setup_exportable_fields(**meta_options)
        return super(ExportableQueryMixin, cls).__init_subclass_with_meta__(
            **meta_options
        )

    @classmethod
    def _setup_exportable_fields(cls, **meta_options):
        for field in cls.get_exportable_fields():
            field_name = f"{field}_export"
            fld = getattr(cls, field)
            new_args = fld.args
            new_args["file_format"] = graphene.String()
            new_args["fields"] = graphene.List(of_type=graphene.String)
            new_args["fields_columns"] = GenericScalar()
            setattr(cls, field_name, graphene.Field(graphene.String, args=new_args))
            cls._create_row_scoped_export_function(field)
            cls._guard_export_fields(field)

    @classmethod
    def _create_row_scoped_export_function(cls, field_name):
        """
        Builds the export over the list resolver restricted, by a primary-key
        subquery, to the rows the exported node type's `get_queryset` keeps, so
        a scope joining a multi-valued relation adds no duplicate line.
        `resolve_<field>` holds the scoped resolver only while
        `create_export_function` runs; an override reading it later gets the
        list's unscoped resolver.
        """
        resolver_name = f"resolve_{field_name}"
        list_resolver = getattr(cls, resolver_name, None)
        if not list_resolver:
            cls.create_export_function(field_name)
            return

        def scoped_resolver(root, info, **kwargs):
            queryset = list_resolver(root, info, **kwargs)
            node_type = cls._unwrap_graphql_type(getattr(cls, field_name).type)
            scoped = node_type.get_queryset(queryset, info)
            if scoped is queryset:
                return queryset
            return queryset.filter(pk__in=scoped.values("pk"))

        has_own_resolver = resolver_name in cls.__dict__
        own_resolver = cls.__dict__.get(resolver_name)
        setattr(cls, resolver_name, scoped_resolver)
        try:
            cls.create_export_function(field_name)
        finally:
            if has_own_resolver:
                setattr(cls, resolver_name, own_resolver)
            else:
                delattr(cls, resolver_name)

    @classmethod
    def _guard_export_fields(cls, field_name):
        """
        Wraps the export resolver so that every requested field is a path the
        exported GraphQL type exposes; any other path is refused before the
        query runs. Subclasses overriding `create_export_function` stay covered.
        """
        resolver_name = f"resolve_{field_name}_export"
        export_resolver = getattr(cls, resolver_name)

        def guarded_exporter(cls, self, info, **kwargs):
            node_type = cls._unwrap_graphql_type(getattr(cls, field_name).type)
            fields = kwargs.get("fields")
            if not fields:
                raise ValueError(f"Field '{fields}' cannot be exported.")
            for field in fields:
                if not isinstance(field, str):
                    raise ValueError(f"Field '{field}' cannot be exported.")
                path = cls._adjust_notation(field).split("__")
                if not cls._is_exposed_path(node_type, path):
                    raise ValueError(f"Field '{field}' cannot be exported.")
            return export_resolver(self, info, **kwargs)

        setattr(cls, resolver_name, types.MethodType(guarded_exporter, cls))

    @classmethod
    def _unwrap_graphql_type(cls, graphql_type):
        while True:
            if isinstance(graphql_type, (graphene.List, graphene.NonNull)):
                graphql_type = graphql_type.of_type
            elif isinstance(graphql_type, type) and issubclass(graphql_type, graphene.relay.Connection):
                graphql_type = graphql_type._meta.node
            else:
                return graphql_type

    @classmethod
    def _is_exposed_path(cls, graphql_type, path):
        """
        A path reads the exported row only: one of its concrete fields, or
        `json_ext` and one key, or one forward foreign key or one-to-one followed
        by one concrete field of the target, or the target's `json_ext` and one
        key. Reverse relations, many-to-many, generic relations and `core` models
        other than a final `username` on `User` are refused. Every segment must
        be a field of the GraphQL type at its level.
        """
        model_field, graphql_field = cls._exposed_field(graphql_type, path[0])
        if model_field is None:
            return False
        rest = path[1:]
        if not model_field.is_relation:
            return cls._is_value_path(model_field, rest)
        if not cls._is_forward_single_relation(model_field):
            return False
        target = model_field.related_model
        if target._meta.app_label == "core" and not (target is User and rest == ["username"]):
            return False
        if not rest:
            return True
        next_type = cls._unwrap_graphql_type(graphql_field.type)
        if not (isinstance(next_type, type) and issubclass(next_type, DjangoObjectType)):
            return False
        if next_type._meta.model is not target:
            return False
        target_field, _ = cls._exposed_field(next_type, rest[0])
        if target_field is None or target_field.is_relation:
            return False
        return cls._is_value_path(target_field, rest[1:])

    @classmethod
    def _exposed_field(cls, graphql_type, name):
        if not (isinstance(graphql_type, type) and issubclass(graphql_type, DjangoObjectType)):
            return None, None
        graphql_field = graphql_type._meta.fields.get(name)
        if isinstance(graphql_field, graphene.Dynamic):
            graphql_field = graphql_field.get_type()
        if graphql_field is None:
            return None, None
        try:
            return graphql_type._meta.model._meta.get_field(name), graphql_field
        except FieldDoesNotExist:
            return None, None

    @classmethod
    def _is_value_path(cls, model_field, rest):
        if not model_field.concrete:
            return False
        if not rest:
            return True
        return (
            model_field.name == "json_ext"
            and isinstance(model_field, models.JSONField)
            and len(rest) == 1
            and bool(_JSON_KEY.fullmatch(rest[0]))
        )

    @classmethod
    def _is_forward_single_relation(cls, model_field):
        return (
            model_field.concrete
            and not model_field.auto_created
            and (model_field.many_to_one or model_field.one_to_one)
        )

    @classmethod
    def _adjust_notation(cls, field):
        from graphene.utils.str_converters import to_snake_case

        return to_snake_case(field.replace(".", "__"))

    @classmethod
    def create_export_function(cls, field_name):
        new_function_name = f"resolve_{field_name}_export"
        default_resolve = getattr(cls, f"resolve_{field_name}", None)

        if not default_resolve:
            raise AttributeError(
                f"Query {cls} doesn't provide resolve function for {field_name}. "
                f"CSV export cannot be created"
            )

        def exporter(cls, self, info, **kwargs):
            custom_filters = kwargs.pop("customFilters", None)
            export_fields = [cls._adjust_notation(f) for f in kwargs.pop("fields")]
            fields_mapping = json.loads(kwargs.pop("fields_columns"))
            file_format = kwargs.pop("file_format", "csv")

            source_field = getattr(cls, field_name)
            filter_kwargs = {
                k: v for k, v in kwargs.items() if k in source_field.filtering_args
            }

            qs = default_resolve(None, info, **kwargs)
            qs = qs.filter(**filter_kwargs)
            qs = cls.__append_custom_filters(custom_filters, qs)
            export_file = ExportableQueryModel.create_csv_export(
                qs,
                export_fields,
                info.context.user,
                column_names=fields_mapping,
                patches=cls.get_patches_for_field(field_name),
                file_format=file_format,
            )

            return export_file.name

        setattr(cls, new_function_name, types.MethodType(exporter, cls))

    @classmethod
    def __append_custom_filters(cls, custom_filters, queryset):
        if custom_filters:
            module_name = cls.get_module_name()
            object_type = cls.get_object_type()
            related_field = cls.get_related_field()
            queryset = CustomFilterWizardStorage.build_custom_filters_queryset(
                module_name,
                object_type,
                custom_filters,
                queryset,
                relation=related_field,
            )
        return queryset
