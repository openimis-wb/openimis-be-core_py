import json
import types

import graphene
from django.core.cache import cache
from django.test import TestCase
from graphene_django import DjangoObjectType

from core.gql.export_mixin import ExportableQueryMixin
from core.models import ExportableQueryModel, MutationLog
from core.schema import MutationLogGQLType, OrderedDjangoFilterConnectionField
from core.test_helpers import create_test_interactive_user, create_test_role
from core.utils import ExtendedConnection


class MutationLogExportTestQuery(ExportableQueryMixin, graphene.ObjectType):
    exportable_fields = ["mutation_logs"]

    mutation_logs = OrderedDjangoFilterConnectionField(
        MutationLogGQLType,
        orderBy=graphene.List(of_type=graphene.String),
    )

    def resolve_mutation_logs(self, info, **kwargs):
        return MutationLog.objects.all()


class UnscopedMutationLogGQLType(DjangoObjectType):
    class Meta:
        model = MutationLog
        name = "UnscopedMutationLogExportTestType"
        skip_registry = True
        interfaces = (graphene.relay.Node,)
        fields = ("id", "client_mutation_id", "client_mutation_label")
        filter_fields = {"client_mutation_id": ["exact"]}
        connection_class = ExtendedConnection


class UnscopedMutationLogExportTestQuery(ExportableQueryMixin, graphene.ObjectType):
    exportable_fields = ["unscoped_mutation_logs"]

    unscoped_mutation_logs = OrderedDjangoFilterConnectionField(
        UnscopedMutationLogGQLType,
        orderBy=graphene.List(of_type=graphene.String),
    )

    def resolve_unscoped_mutation_logs(self, info, **kwargs):
        return MutationLog.objects.all()


class ExportFieldsTestCase(TestCase):
    password = "EdfmD3!12@#"
    client_mutation_id = "export-fields-test"
    schema = graphene.Schema(query=MutationLogExportTestQuery)

    @classmethod
    def setUpTestData(cls):
        cls.requester = create_test_interactive_user(username="export_requester", password=cls.password)
        cls.other_user = create_test_interactive_user(username="export_other_user", password=cls.password)
        cls.other_user.i_user.set_password(cls.password, private_key="a1" * 64)
        cls.other_user.i_user.email = "other.user@example.invalid"
        cls.other_user.i_user.save()
        MutationLog.objects.create(
            json_content="{}",
            user=cls.other_user,
            client_mutation_id=cls.client_mutation_id,
            client_mutation_label="export fields test",
            json_ext={"k1": "json-value-1"},
        )

    def _export(self, fields):
        query = """
            query ($clientMutationId: String, $fields: [String]) {
                mutationLogsExport(clientMutationId: $clientMutationId, fields: $fields, fieldsColumns: "{}")
            }
        """
        return self.schema.execute(
            query,
            variables={"clientMutationId": self.client_mutation_id, "fields": fields},
            context_value=types.SimpleNamespace(user=self.requester),
        )

    def _exported_files(self):
        contents = []
        for export in ExportableQueryModel.objects.filter(user=self.requester):
            contents.append(export.content.read().decode())
            export.content.delete(save=False)
            export.delete()
        return contents

    def _assert_refused(self, field):
        result = self._export(["id", field])
        contents = self._exported_files()
        self.assertIsNotNone(result.errors)
        self.assertIn("cannot be exported", str(result.errors[0]))
        self.assertEqual(contents, [])

    def test_refuses_credentials_of_a_related_user(self):
        private_key = self.other_user.i_user.private_key
        password = self.other_user.i_user.password
        for fields in (["id", "user.iUser.privateKey"], ["user__i_user__password"]):
            with self.subTest(fields=fields):
                result = self._export(fields)
                contents = self._exported_files()
                self.assertFalse(
                    any(private_key in c or password in c for c in contents),
                    "the export file carries another user's credentials",
                )
                self.assertIsNotNone(result.errors)
                self.assertEqual(contents, [])

    def test_refuses_account_data_reached_through_a_user(self):
        for field in (
            "user.iUser.loginName",
            "user.iUser.email",
            "user__i_user__phone",
            "user.dataExports.name",
            "user.id",
            "user",
            "user.username.lower",
        ):
            with self.subTest(field=field):
                self._assert_refused(field)

    def test_refuses_paths_outside_the_exported_type(self):
        for field in ("pk", "user____username", "", "client_mutation_label.lower"):
            with self.subTest(field=field):
                self._assert_refused(field)

    def test_refuses_json_paths_other_than_one_simple_key(self):
        for field in ("json_ext__k1__k2", "json_ext__bad-key", "json_ext__", "json_ext__1k"):
            with self.subTest(field=field):
                self._assert_refused(field)

    def test_refuses_missing_empty_and_null_fields(self):
        query = """
            query ($clientMutationId: String) {
                mutationLogsExport(clientMutationId: $clientMutationId, fieldsColumns: "{}")
            }
        """
        missing = self.schema.execute(
            query,
            variables={"clientMutationId": self.client_mutation_id},
            context_value=types.SimpleNamespace(user=self.requester),
        )
        for result in (missing, self._export([]), self._export([None]), self._export(["id", None])):
            with self.subTest(errors=result.errors):
                contents = self._exported_files()
                self.assertIsNotNone(result.errors)
                self.assertIn("cannot be exported", str(result.errors[0]))
                self.assertEqual(contents, [])

    def test_exports_fields_exposed_by_the_type(self):
        result = self._export(["id", "client_mutation_label", "user.username", "json_ext__k1"])

        self.assertIsNone(result.errors)
        contents = self._exported_files()
        self.assertEqual(len(contents), 1)
        self.assertIn(self.other_user.username, contents[0])
        self.assertIn("export fields test", contents[0])
        self.assertIn("json-value-1", contents[0])


ROW_SCOPE_MUTATION_ID = "export-row-scope-test"


class JoiningScopeMutationLogGQLType(DjangoObjectType):
    """Scopes through a multi-valued relation: each row joins every mutation log of its user."""

    class Meta:
        model = MutationLog
        name = "JoiningScopeMutationLogExportTestType"
        skip_registry = True
        interfaces = (graphene.relay.Node,)
        fields = ("id", "client_mutation_id", "client_mutation_label")
        filter_fields = {"client_mutation_id": ["exact"]}
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        return queryset.filter(user=info.context.user, user__mutationlog__client_mutation_id=ROW_SCOPE_MUTATION_ID)


class JoiningScopeExportTestQuery(ExportableQueryMixin, graphene.ObjectType):
    exportable_fields = ["joining_mutation_logs"]

    joining_mutation_logs = OrderedDjangoFilterConnectionField(
        JoiningScopeMutationLogGQLType,
        orderBy=graphene.List(of_type=graphene.String),
    )

    def resolve_joining_mutation_logs(self, info, **kwargs):
        return MutationLog.objects.all()


class OverridingExporterMixin(ExportableQueryMixin):
    """An exporter override that reads the list resolver while it builds, as social_protection's does."""

    @classmethod
    def create_export_function(cls, field_name):
        default_resolve = getattr(cls, f"resolve_{field_name}", None)

        def exporter(cls, self, info, **kwargs):
            export_fields = [cls._adjust_notation(f) for f in kwargs.pop("fields")]
            fields_mapping = json.loads(kwargs.pop("fields_columns"))
            source_field = getattr(cls, field_name)
            filter_kwargs = {k: v for k, v in kwargs.items() if k in source_field.filtering_args}
            qs = default_resolve(None, info, **kwargs).filter(**filter_kwargs)
            return ExportableQueryModel.create_csv_export(
                qs, export_fields, info.context.user, column_names=fields_mapping).name

        setattr(cls, f"resolve_{field_name}_export", types.MethodType(exporter, cls))


class OverriddenExporterTestQuery(OverridingExporterMixin, graphene.ObjectType):
    exportable_fields = ["overridden_mutation_logs"]

    overridden_mutation_logs = OrderedDjangoFilterConnectionField(
        MutationLogGQLType,
        orderBy=graphene.List(of_type=graphene.String),
    )

    def resolve_overridden_mutation_logs(self, info, **kwargs):
        return MutationLog.objects.all()


class InheritedResolverTestQuery(OverriddenExporterTestQuery):
    """Re-runs the export setup with an inherited list resolver, as the assembled schema's Query does."""


class ExportRowScopeTestCase(TestCase):
    """
    The export keeps the rows the exported node type's `get_queryset` keeps:
    `MutationLogGQLType` shows a non-superuser only their own mutation logs.
    """

    password = "EdfmD3!12@#"
    client_mutation_id = ROW_SCOPE_MUTATION_ID
    scoped_schema = graphene.Schema(query=MutationLogExportTestQuery)
    unscoped_schema = graphene.Schema(query=UnscopedMutationLogExportTestQuery)
    overridden_schema = graphene.Schema(query=OverriddenExporterTestQuery)
    inherited_schema = graphene.Schema(query=InheritedResolverTestQuery)
    joining_schema = graphene.Schema(query=JoiningScopeExportTestQuery)

    @classmethod
    def setUpTestData(cls):
        plain_role = create_test_role(perm_names=[], name="ExportRowScopeNoAdmin")
        cls.restricted = create_test_interactive_user(
            username="export_scope_restricted", password=cls.password, roles=[plain_role.id])
        cls.superuser = create_test_interactive_user(username="export_scope_superuser", password=cls.password)
        cls.other_user = create_test_interactive_user(
            username="export_scope_other", password=cls.password, roles=[plain_role.id])
        for user, label in (
            (cls.restricted, "scope-own-log"),
            (cls.restricted, "scope-own-second-log"),
            (cls.other_user, "scope-other-log"),
        ):
            MutationLog.objects.create(
                json_content="{}", user=user, client_mutation_id=cls.client_mutation_id,
                client_mutation_label=label)
        cache.clear()

    def _export(self, schema, query_name, requester):
        query = """
            query ($clientMutationId: String) {
                %s(clientMutationId: $clientMutationId, fields: ["id", "client_mutation_label"],
                   fieldsColumns: "{}")
            }
        """ % query_name
        result = schema.execute(
            query,
            variables={"clientMutationId": self.client_mutation_id},
            context_value=types.SimpleNamespace(user=requester),
        )
        self.assertIsNone(result.errors)
        contents = []
        for export in ExportableQueryModel.objects.filter(user=requester):
            contents.append(export.content.read().decode())
            export.content.delete(save=False)
            export.delete()
        self.assertEqual(len(contents), 1)
        return contents[0]

    def test_preconditions(self):
        self.assertFalse(self.restricted.is_superuser)
        self.assertTrue(self.superuser.is_superuser)

    def test_export_keeps_only_the_rows_the_node_type_scopes(self):
        content = self._export(self.scoped_schema, "mutationLogsExport", self.restricted)

        self.assertIn("scope-own-log", content)
        self.assertNotIn("scope-other-log", content)

    def test_superuser_export_keeps_every_row(self):
        content = self._export(self.scoped_schema, "mutationLogsExport", self.superuser)

        self.assertIn("scope-own-log", content)
        self.assertIn("scope-other-log", content)

    def test_default_get_queryset_leaves_the_rows_unchanged(self):
        content = self._export(self.unscoped_schema, "unscopedMutationLogsExport", self.restricted)

        self.assertIn("scope-own-log", content)
        self.assertIn("scope-other-log", content)

    def test_scope_through_a_multi_valued_relation_keeps_one_line_per_row(self):
        content = self._export(self.joining_schema, "joiningMutationLogsExport", self.restricted)

        self.assertEqual(content.count("scope-own-log"), 1)
        self.assertEqual(content.count("scope-own-second-log"), 1)
        self.assertNotIn("scope-other-log", content)
        self.assertEqual(len(content.splitlines()), 3)

    def test_overriding_exporter_keeps_only_the_rows_the_node_type_scopes(self):
        for schema in (self.overridden_schema, self.inherited_schema):
            with self.subTest(query=schema.get_query_type().name):
                content = self._export(schema, "overriddenMutationLogsExport", self.restricted)

                self.assertIn("scope-own-log", content)
                self.assertNotIn("scope-other-log", content)

    def test_list_resolvers_stay_unscoped(self):
        self.assertEqual(
            MutationLogExportTestQuery.__dict__["resolve_mutation_logs"].__qualname__,
            "MutationLogExportTestQuery.resolve_mutation_logs",
        )
        self.assertEqual(
            OverriddenExporterTestQuery.__dict__["resolve_overridden_mutation_logs"].__qualname__,
            "OverriddenExporterTestQuery.resolve_overridden_mutation_logs",
        )
        self.assertNotIn("resolve_overridden_mutation_logs", InheritedResolverTestQuery.__dict__)
