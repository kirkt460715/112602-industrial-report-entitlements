from http.client import HTTPConnection
import json
from threading import Thread
import unittest

from backend import Desk, DeskApi, FeedClient
from partner_feed import PartnerFeed


def serve(server):
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    return thread


class DeskTestBase(unittest.TestCase):
    def setUp(self):
        self.feed_server = PartnerFeed(0)
        self.addCleanup(self.feed_server.server_close)
        thread = serve(self.feed_server)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.feed_server.shutdown)
        self.desk = Desk(FeedClient("127.0.0.1", self.feed_server.server_port))
        self.desk.load_register()


class ScenarioFlowTest(DeskTestBase):
    def test_tier_license_delivery_flow(self):
        desk = self.desk
        desk.sync()
        self.assertEqual(desk.cursor, 5)
        # 档位已切换到 expanded，旧档受理的占用延续过来，不重复记账
        quota = desk.quota("factory-a")
        self.assertEqual((quota["plan"], quota["total"]), ("expanded", 7))
        self.assertEqual((quota["occupied"], quota["consumed"]), (2, 0))
        # 交付已登记，但回执首次查询 503，状态未核实仍须占用
        order = desk.orders["report-1"]
        self.assertEqual(order["state"], "delivered")
        delivery = desk.deliveries[("report-1", "package-7")]
        self.assertEqual(delivery["status"], "unverified")
        # 受托方已退出，凭旧查询不能取包
        status, body = desk.delegate_fetch("repair-v", "report-1")
        self.assertEqual((status, body["error"]), (403, "delegate_withdrawn"))
        # 供应方权利确认已取回：shift-east 仅限 internal
        self.assertEqual(desk.rights["shift-east"]["purpose"], ["internal"])

        # 回执晚于受托方退出才核实：实发对象与版本留痕
        desk.sync()
        delivery = desk.deliveries[("report-1", "package-7")]
        self.assertEqual(delivery["status"], "verified")
        self.assertEqual(delivery["recipient"], "repair-v")
        self.assertEqual(delivery["sent_at"], 13)
        self.assertEqual(delivery["version"], desk.versions["package-7"])
        self.assertEqual(desk.orders["report-1"]["state"], "verified")
        quota = desk.quota("factory-a")
        self.assertEqual((quota["occupied"], quota["consumed"], quota["available"]), (0, 2, 5))

        # 同一实际交付重复到达不重复占额
        desk.apply({"sequence": 6, "at": 22, "source": "dispatch",
                    "facts": {"order": "report-1", "sent_at": 13, "artifact": "package-7"}})
        self.assertEqual(len(desk.deliveries), 1)
        self.assertEqual(desk.quota("factory-a")["consumed"], 2)

        # 供应方查自己的授权被用于哪些交付
        usage = desk.supplier_usage("power")
        self.assertEqual(len(usage["deliveries"]), 1)
        self.assertEqual(usage["deliveries"][0]["recipient"], "repair-v")
        self.assertFalse(usage["grant"]["new_issuance"])
        self.assertTrue(usage["grant"]["existing_retention"])
        self.assertEqual(usage["grant"]["narrowed_at"], 14)

        # 工厂查可继续领取的报告、未兑现的额度和收费依据
        account = desk.factory_account("factory-a")
        self.assertEqual([r["artifact"] for r in account["receivable"]], ["package-7"])
        self.assertEqual(account["receivable"][0]["recipient"], "repair-v")
        self.assertEqual(account["quota"]["available"], 5)
        self.assertEqual(account["billing"]["plan"], "expanded")
        self.assertEqual(account["billing"]["plan_history"],
                         [{"effective": None, "plan": "standard"},
                          {"effective": 10, "plan": "expanded"}])
        self.assertEqual(account["billing"]["units_consumed"], 2)
        # report-2 因许可受限未兑现，原因可查
        unfulfilled = {u["order"]: u for u in account["unfulfilled"]}
        self.assertIn("report-2", unfulfilled)
        reasons = unfulfilled["report-2"]["reasons"]
        self.assertIn("new_issuance_closed", reasons["power"])
        self.assertIn("purpose_not_allowed", reasons["shift-east"])

    def test_reduction_requires_confirmation(self):
        desk = self.desk
        desk.sync()
        # 受托查阅 auditor 用途下没有任何合规范围
        status, body = desk.reduce("report-2")
        self.assertEqual((status, body["error"]), (422, "no_compliant_scope"))
        # 缩减为工厂自读：shift-east 合规、power 被砍，须先确认范围不足
        status, body = desk.reduce("report-2", purpose="internal", delegate=None)
        self.assertEqual((status, body["error"]), (409, "confirmation_required"))
        self.assertEqual(body["acceptable"], ["shift-east"])
        self.assertEqual(body["insufficient"][0]["source"], "power")
        # 用户确认后受理，占用额度
        status, body = desk.reduce("report-2", purpose="internal", delegate=None,
                                   units=1, confirm_insufficient=True)
        self.assertEqual(status, 200)
        self.assertEqual(body["sources"], ["shift-east"])
        order = desk.orders["report-2"]
        self.assertEqual((order["state"], order["units"], order["delegate"]),
                         ("accepted", 1, None))
        self.assertEqual(desk.quota("factory-a")["occupied"], 3)  # 未核实交付 2 + 缩减 1
        # 未交付的缩减单撤销后释放额度
        status, body = desk.cancel("report-2")
        self.assertEqual((status, body["released"]), (200, 1))
        self.assertEqual(desk.quota("factory-a")["occupied"], 2)
        # 已交付的不可撤销
        status, body = desk.cancel("report-1")
        self.assertEqual((status, body["error"]), (409, "already_delivered"))


class DelegateAccessTest(DeskTestBase):
    def test_delegate_fetch_before_and_after_withdrawal(self):
        desk = self.desk
        desk.apply({"sequence": 1, "at": 7, "source": "request",
                    "facts": {"order": "report-1", "state": "accepted", "units": 2}})
        desk.apply({"sequence": 5, "at": 21, "source": "dispatch",
                    "facts": {"order": "report-1", "sent_at": 13, "artifact": "package-7"}})
        # 受托方在退出前可以取到发给自己的包
        status, body = desk.delegate_fetch("repair-v", "report-1")
        self.assertEqual(status, 200)
        self.assertEqual(body["packages"][0]["artifact"], "package-7")
        self.assertEqual(body["packages"][0]["values"], {"energy_units": 158})
        # 非受托方不能取
        self.assertEqual(desk.delegate_fetch("repair-x", "report-1")[0], 403)
        # 退出后旧查询不能再取
        desk.apply({"sequence": 4, "at": 17, "source": "client",
                    "facts": {"delegate": "repair-v", "withdrawn": True}})
        status, body = desk.delegate_fetch("repair-v", "report-1")
        self.assertEqual((status, body["error"]), (403, "delegate_withdrawn"))


class LicenseCheckTest(DeskTestBase):
    def test_purpose_and_delegate_rules(self):
        desk = self.desk
        desk.sync()
        # shift-east 只允许 internal：受托查阅 auditor 被拒
        check = desk.evaluate_source("shift-east", "auditor", "repair-v", desk.clock)
        self.assertFalse(check["allowed"])
        self.assertIn("purpose_not_allowed", check["reasons"])
        self.assertTrue(check["confirmed"])
        # 工厂自读 internal 合规
        check = desk.evaluate_source("shift-east", "internal", None, desk.clock)
        self.assertTrue(check["allowed"])
        # 受托方不能落在自读用途上
        check = desk.evaluate_source("shift-east", "internal", "repair-v", desk.clock)
        self.assertIn("delegate_reading_internal_only", check["reasons"])
        # power 已限制新出包，但保留已签包
        check = desk.evaluate_source("power", "auditor", None, desk.clock)
        self.assertIn("new_issuance_closed", check["reasons"])

    def test_grain_site_validity_rules(self):
        desk = Desk(None)
        desk.load_register_data({
            "customers": ["f"],
            "sites": ["s1", "s2"],
            "grants": [
                {"source": "g", "scope": "aggregate", "purpose": ["internal"],
                 "sites": ["s1"], "valid_to": 5},
            ],
            "offers": {"standard": {"units": 1, "parallel": 1}},
            "orders": [],
            "artifacts": {
                "a1": {"site": "s2", "month": "m", "source": "g",
                       "grain": "shift", "values": {}},
            },
        })
        # 汇总级授权不能放行班次细项，也不能覆盖授权外厂区
        check = desk.evaluate_source("g", "internal", None, 3)
        self.assertEqual(check["reasons"], ["grain_exceeds_scope", "site_not_covered"])
        # 有效期外不可用
        check = desk.evaluate_source("g", "internal", None, 7)
        self.assertIn("expired", check["reasons"])
        # 未知来源
        check = desk.evaluate_source("missing", "internal", None, 1)
        self.assertEqual(check["reasons"], ["unknown_source"])


class DeskApiTest(DeskTestBase):
    def setUp(self):
        super().setUp()
        self.api = DeskApi(0, self.desk)
        self.addCleanup(self.api.server_close)
        thread = serve(self.api)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.api.shutdown)

    def call(self, path, method="GET", body=None):
        connection = HTTPConnection("127.0.0.1", self.api.server_port, timeout=2)
        try:
            payload = json.dumps(body).encode("utf-8") if body is not None else None
            headers = {"Content-Type": "application/json"} if body is not None else {}
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_http_endpoints(self):
        self.assertEqual(self.call("/sync", "POST")[0], 200)
        self.assertEqual(self.call("/sync", "POST")[0], 200)

        status, account = self.call("/factory/factory-a/account")
        self.assertEqual(status, 200)
        self.assertEqual(account["billing"]["plan"], "expanded")
        self.assertEqual(account["quota"]["consumed"], 2)
        self.assertEqual([r["artifact"] for r in account["receivable"]], ["package-7"])

        status, usage = self.call("/supplier/power/usage")
        self.assertEqual(status, 200)
        self.assertEqual(usage["deliveries"][0]["recipient"], "repair-v")

        status, record = self.call("/deliveries/report-1")
        self.assertEqual(status, 200)
        self.assertEqual(record["deliveries"][0]["version"], self.desk.versions["package-7"])
        self.assertEqual(record["deliveries"][0]["values"], {"energy_units": 158})
        self.assertEqual(record["deliveries"][0]["status"], "verified")

        status, denied = self.call("/delegate/repair-v/packages/report-1")
        self.assertEqual((status, denied["error"]), (403, "delegate_withdrawn"))

        status, detail = self.call("/operator/orders/report-2")
        self.assertEqual(status, 200)
        reasons = {c["source"]: c["reasons"] for c in detail["evaluation"]}
        self.assertIn("new_issuance_closed", reasons["power"])
        self.assertIn("purpose_not_allowed", reasons["shift-east"])

        status, body = self.call("/operator/orders/report-2/reduce", "POST",
                                 {"purpose": "internal", "delegate": None})
        self.assertEqual((status, body["error"]), (409, "confirmation_required"))
        status, body = self.call("/operator/orders/report-2/reduce", "POST",
                                 {"purpose": "internal", "delegate": None,
                                  "units": 1, "confirm_insufficient": True})
        self.assertEqual(status, 200)
        self.assertEqual(body["sources"], ["shift-east"])
        status, body = self.call("/operator/orders/report-2/cancel", "POST")
        self.assertEqual((status, body["released"]), (200, 1))

        status, overview = self.call("/operator/overview")
        self.assertEqual(status, 200)
        self.assertEqual(overview["orders"]["report-1"]["state"], "verified")
        self.assertEqual(overview["delegates"]["repair-v"]["withdrawn"], True)

        self.assertEqual(self.call("/factory/unknown/account")[0], 404)
        self.assertEqual(self.call("/deliveries/report-2")[0], 404)


if __name__ == "__main__":
    unittest.main()
