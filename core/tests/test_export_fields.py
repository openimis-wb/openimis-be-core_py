import types

import graphene
from django.test import TestCase

from core.gql.export_mixin import ExportableQueryMixin
from core.gql_queries import UserGQLType
from core.models import ExportableQueryModel, User
from core.schema import OrderedDjangoFilterConnectionField
from core.test_helpers import create_test_interactive_user


class UserExportTestQuery(ExportableQueryMixin, graphene.ObjectType):
    exportable_fields = ["users"]

    users = OrderedDjangoFilterConnectionField(
        UserGQLType,
        orderBy=graphene.List(of_type=graphene.String),
    )

    def resolve_users(self, info, **kwargs):
        return User.objects.all()


class ExportFieldsTestCase(TestCase):
    password = "EdfmD3!12@#"
    schema = graphene.Schema(query=UserExportTestQuery)

    @classmethod
    def setUpTestData(cls):
        cls.requester = create_test_interactive_user(username="export_requester", password=cls.password)
        cls.other_user = create_test_interactive_user(username="export_other_user", password=cls.password)
        cls.other_user.i_user.set_password(cls.password, private_key="a1" * 64)
        cls.other_user.i_user.save()

    def _export(self, fields):
        query = """
            query ($username: String, $fields: [String]) {
                usersExport(username: $username, fields: $fields, fieldsColumns: "{}")
            }
        """
        return self.schema.execute(
            query,
            variables={"username": self.other_user.username, "fields": fields},
            context_value=types.SimpleNamespace(user=self.requester),
        )

    def _exported_files(self):
        contents = []
        for export in ExportableQueryModel.objects.filter(user=self.requester):
            contents.append(export.content.read().decode())
            export.content.delete(save=False)
            export.delete()
        return contents

    def test_refuses_credentials_of_a_related_user(self):
        private_key = self.other_user.i_user.private_key
        password = self.other_user.i_user.password
        for fields in (["username", "iUser.privateKey"], ["i_user__password"]):
            with self.subTest(fields=fields):
                result = self._export(fields)
                contents = self._exported_files()
                self.assertFalse(
                    any(private_key in c or password in c for c in contents),
                    "the export file carries another user's credentials",
                )
                self.assertIsNotNone(result.errors)
                self.assertEqual(contents, [])

    def test_refuses_paths_outside_the_exported_type(self):
        for field in ("tUser.password", "username.length", "pk", "i_user____id", ""):
            with self.subTest(field=field):
                result = self._export(["id", field])
                self.assertIsNotNone(result.errors)
                self.assertIn("cannot be exported", str(result.errors[0]))
                self.assertEqual(self._exported_files(), [])

    def test_exports_fields_exposed_by_the_type(self):
        result = self._export(["id", "username", "iUser.lastName", "i_user__login_name"])

        self.assertIsNone(result.errors)
        contents = self._exported_files()
        self.assertEqual(len(contents), 1)
        self.assertIn(self.other_user.username, contents[0])
        self.assertIn(self.other_user.i_user.last_name, contents[0])
