import types

import graphene
from django.test import TestCase

from core.gql.export_mixin import ExportableQueryMixin
from core.models import ExportableQueryModel, MutationLog
from core.schema import MutationLogGQLType, OrderedDjangoFilterConnectionField
from core.test_helpers import create_test_interactive_user


class MutationLogExportTestQuery(ExportableQueryMixin, graphene.ObjectType):
    exportable_fields = ["mutation_logs"]

    mutation_logs = OrderedDjangoFilterConnectionField(
        MutationLogGQLType,
        orderBy=graphene.List(of_type=graphene.String),
    )

    def resolve_mutation_logs(self, info, **kwargs):
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

    def test_exports_fields_exposed_by_the_type(self):
        result = self._export(["id", "client_mutation_label", "user.username", "json_ext__k1"])

        self.assertIsNone(result.errors)
        contents = self._exported_files()
        self.assertEqual(len(contents), 1)
        self.assertIn(self.other_user.username, contents[0])
        self.assertIn("export fields test", contents[0])
        self.assertIn("json-value-1", contents[0])
