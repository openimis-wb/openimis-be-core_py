import types
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.test import TestCase, override_settings

from core.models import ExportableQueryModel
from core.test_helpers import create_test_interactive_user, create_test_role
from individual.models import Group, GroupIndividual, Individual
from invoice.models import Bill
from location.test_helpers import assign_user_districts, create_test_village
from social_protection.export_mixin import ExportableSocialProtectionQueryMixin
from social_protection.models import BeneficiaryStatus, Beneficiary, BenefitPlan, GroupBeneficiary

# Field lists the web client sends for each export.
CLIENT_EXPORTS = {
    "individualExport": ["id", "first_name", "last_name", "dob", "json_ext__k1"],
    "groupExport": ["id", "json_ext"],
    "groupIndividualExport": [
        "individual__id", "individual__first_name", "individual__last_name", "individual__dob", "role", "json_ext",
    ],
    "beneficiaryExport": ["id", "individual.first_name", "individual.last_name", "individual.dob", "json_ext"],
    "groupBeneficiaryExport": ["id", "group.id", "json_ext"],
    "billExport": [
        "id", "userCreated.username", "amount_total", "subjectType", "subjectId",
        "thirdpartyType", "thirdpartyId", "code", "status", "dateBill",
    ],
}

# Fields the export patches of a query read, sent alongside each refused field.
PATCH_FIELDS = {
    "beneficiaryExport": ["id", "json_ext"],
    "groupBeneficiaryExport": ["id", "json_ext"],
    "billExport": ["id", "subjectType", "subjectId"],
}

REFUSED_FIELDS = {
    "individualExport": [
        "userCreated.iUser.privateKey",
        "user_created__i_user__password",
        "location.parent.parent.parent.children.children.children.individuals.firstName",
        "location.parent.code",
        "groupindividuals.role",
        "json_ext__k1__k2",
    ],
    "groupExport": ["userCreated.iUser.privateKey", "groupindividuals.individual.firstName"],
    "groupIndividualExport": ["individual__user_created__i_user__private_key", "group.groupindividuals.role"],
    "beneficiaryExport": [
        "individual.userCreated.iUser.privateKey",
        "user_created__i_user__password",
        "individual.groupindividuals.role",
        "benefitPlan.beneficiaryDataSchema.k1",
    ],
    "groupBeneficiaryExport": ["group.userCreated.iUser.privateKey", "group.groupindividuals.role"],
    "billExport": ["userCreated.iUser.privateKey", "userCreated.iUser.password", "subject", "thirdparty"],
}


class ExportFieldsSchemaTestCase(TestCase):
    password = "EdfmD3!12@#"

    @classmethod
    def setUpTestData(cls):
        cls.requester = create_test_interactive_user(username="export_schema_requester", password=cls.password)
        cls.other_user = create_test_interactive_user(username="export_schema_other", password=cls.password)
        cls.other_user.i_user.set_password(cls.password, private_key="d4" * 64)
        cls.other_user.i_user.save()

        individual = Individual(first_name="ExportFirst", last_name="ExportLast", dob="1990-01-01",
                                json_ext={"k1": "individual-k1"})
        individual.save(user=cls.other_user)
        group = Group(code="EXPGRP01", json_ext={"k1": "v"})
        group.save(user=cls.other_user)
        GroupIndividual(group=group, individual=individual, role="HEAD", json_ext={"k1": "v"}).save(
            user=cls.other_user)
        individual_plan = BenefitPlan(
            code="EXPIND01", name="EXPIND01", max_beneficiaries=10, ceiling_per_beneficiary=Decimal("1.00"),
            type=BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE, beneficiary_data_schema={})
        individual_plan.save(user=cls.other_user)
        group_plan = BenefitPlan(
            code="EXPGRP01", name="EXPGRP01", max_beneficiaries=10, ceiling_per_beneficiary=Decimal("1.00"),
            type=BenefitPlan.BenefitPlanType.GROUP_TYPE, beneficiary_data_schema={})
        group_plan.save(user=cls.other_user)
        Beneficiary(individual=individual, benefit_plan=individual_plan, status=BeneficiaryStatus.ACTIVE,
                    json_ext={"k1": "v"}).save(user=cls.other_user)
        GroupBeneficiary(group=group, benefit_plan=group_plan, status=BeneficiaryStatus.ACTIVE,
                         json_ext={"k1": "v"}).save(user=cls.other_user)
        individual_type = ContentType.objects.get_for_model(Individual)
        Bill(subject_type=individual_type, subject_id=individual.id, thirdparty_type=individual_type,
             thirdparty_id=individual.id, code="EXPBILL01", date_bill=date(2026, 1, 1),
             amount_total=Decimal("1.00"), amount_net=Decimal("1.00"), json_ext={}).save(user=cls.other_user)

        cls.filters = {
            "individualExport": 'firstName_Iexact: "ExportFirst"',
            "groupExport": 'code_Iexact: "EXPGRP01"',
            "groupIndividualExport": 'group_Id: "%s"' % group.id,
            "beneficiaryExport": 'benefitPlan_Id: "%s"' % individual_plan.id,
            "groupBeneficiaryExport": 'group_Id: "%s"' % group.id,
            "billExport": 'code: "EXPBILL01"',
        }

    def _export(self, query_name, fields):
        from openIMIS.schema import schema

        query = 'query ($fields: [String]) { %s(%s, fields: $fields, fieldsColumns: "{}") }' % (
            query_name, self.filters[query_name])
        result = schema.execute(
            query, variables={"fields": fields}, context_value=types.SimpleNamespace(user=self.requester))
        contents = []
        for export in ExportableQueryModel.objects.filter(user=self.requester):
            contents.append(export.content.read().decode())
            export.content.delete(save=False)
            export.delete()
        return result, contents

    def test_client_field_lists_export(self):
        for query_name, fields in CLIENT_EXPORTS.items():
            with self.subTest(query=query_name):
                result, contents = self._export(query_name, fields)
                self.assertIsNone(result.errors)
                self.assertEqual(len(contents), 1)
                self.assertGreater(len(contents[0].splitlines()), 1)

    def test_individual_export_carries_json_key_and_bill_export_the_username(self):
        _, individual_contents = self._export("individualExport", ["id", "json_ext__k1"])
        _, bill_contents = self._export("billExport", ["id", "subjectType", "subjectId", "userCreated.username"])

        self.assertIn("individual-k1", individual_contents[0])
        self.assertIn(self.other_user.username, bill_contents[0])

    def test_refused_fields(self):
        secrets = (self.other_user.i_user.private_key, self.other_user.i_user.password)
        for query_name, fields in REFUSED_FIELDS.items():
            for field in fields:
                with self.subTest(query=query_name, field=field):
                    result, contents = self._export(query_name, PATCH_FIELDS.get(query_name, ["id"]) + [field])
                    self.assertFalse(any(secret in c for secret in secrets for c in contents))
                    self.assertIsNotNone(result.errors)
                    self.assertIn("cannot be exported", str(result.errors[0]))
                    self.assertEqual(contents, [])

    def test_refuses_empty_and_null_fields(self):
        for query_name in CLIENT_EXPORTS:
            for fields in ([], [None]):
                with self.subTest(query=query_name, fields=fields):
                    result, contents = self._export(query_name, fields)
                    self.assertIsNotNone(result.errors)
                    self.assertIn("cannot be exported", str(result.errors[0]))
                    self.assertEqual(contents, [])

    def test_beneficiary_exports_use_the_social_protection_exporter(self):
        self.assertIn("create_export_function", ExportableSocialProtectionQueryMixin.__dict__)
        result, _ = self._export("beneficiaryExport", ["id", "json_ext", "individual.groupindividuals.role"])
        self.assertIn("cannot be exported", str(result.errors[0]))


@override_settings(ROW_SECURITY=True)
class GroupIndividualExportRowScopeTestCase(TestCase):
    """
    groupIndividualExport keeps the members the group member list shows: an
    account limited to some districts exports no member of a group located
    in another district.
    """

    password = "EdfmD3!12@#"

    @classmethod
    def setUpTestData(cls):
        own_village = create_test_village({"code": "EXSCA"})
        other_village = create_test_village({"code": "EXSCB"})
        group_search_role = create_test_role(perm_names=["gql_group_search_perms"], name="ExportRowScopeGroupSearch")
        cls.restricted = create_test_interactive_user(
            username="export_scope_district", password=cls.password, roles=[group_search_role.id])
        assign_user_districts(cls.restricted, ["D-EXSCA"])
        cls.superuser = create_test_interactive_user(username="export_scope_admin", password=cls.password)
        cls.own_group = cls._group_with_member("EXSCGRPA", own_village, "InDistrictMember")
        cls.other_group = cls._group_with_member("EXSCGRPB", other_village, "OutOfDistrictMember")
        cache.clear()

    @classmethod
    def _group_with_member(cls, code, village, first_name):
        group = Group(code=code, location=village, json_ext={})
        group.save(user=cls.superuser)
        individual = Individual(first_name=first_name, last_name="ExportScope", dob="1990-01-01", json_ext={})
        individual.save(user=cls.superuser)
        GroupIndividual(group=group, individual=individual, role="HEAD", json_ext={}).save(user=cls.superuser)
        return group

    def _export(self, requester, group):
        from openIMIS.schema import schema

        query = 'query { groupIndividualExport(group_Id: "%s", fields: ["individual__first_name"], ' \
                'fieldsColumns: "{}") }' % group.id
        result = schema.execute(query, context_value=types.SimpleNamespace(user=requester))
        contents = []
        for export in ExportableQueryModel.objects.filter(user=requester):
            contents.append(export.content.read().decode())
            export.content.delete(save=False)
            export.delete()
        return result, contents

    def test_preconditions(self):
        self.assertFalse(self.restricted.is_imis_admin)
        self.assertTrue(self.superuser.is_imis_admin)

    def test_restricted_account_exports_the_members_of_a_group_in_its_district(self):
        result, contents = self._export(self.restricted, self.own_group)

        self.assertIsNone(result.errors)
        self.assertEqual(len(contents), 1)
        self.assertIn("InDistrictMember", contents[0])

    def test_restricted_account_exports_no_member_of_a_group_in_another_district(self):
        _, contents = self._export(self.restricted, self.other_group)

        self.assertFalse(any("OutOfDistrictMember" in c for c in contents))

    def test_superuser_exports_the_members_of_a_group_in_any_district(self):
        result, contents = self._export(self.superuser, self.other_group)

        self.assertIsNone(result.errors)
        self.assertEqual(len(contents), 1)
        self.assertIn("OutOfDistrictMember", contents[0])
