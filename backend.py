"""园区经办后端：档位变更、资料许可与报告交付的连贯办理。

资料端（partner_feed.py）只提供台账、到达记录和对端回执，不办理业务。
本模块消费这些资料，维护订单、档位、许可、额度与交付的一致台账，
并向工厂、供应方、受托方和园区经办提供查询与办理接口。
报表内容取自台账中的合成产物，本服务不含分析模型。
"""

from __future__ import annotations

import argparse
from hashlib import sha256
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import RLock
from urllib.parse import urlencode, urlsplit

# 工件粒度归入的许可级别：汇总级授权不能放行班次级细项。
GRAIN_LEVEL = {"monthly": "aggregate", "shift": "detail"}
# 工厂自读用途；受托查阅不能落在自读用途上。
SELF_PURPOSE = "internal"
# reduce 未传 delegate 时保持原值的哨兵。
KEEP = object()


class FeedClient:
    """资料端只读客户端。"""

    def __init__(self, host, port):
        self.host = host
        self.port = port

    @classmethod
    def from_url(cls, base):
        url = urlsplit(base if "://" in base else f"http://{base}")
        return cls(url.hostname or "127.0.0.1", url.port or 80)

    def get(self, path, params=None):
        if params:
            path += "?" + urlencode(params)
        connection = HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()


class Desk:
    """经办台账：订单、档位、许可、额度与交付的一致视图。"""

    def __init__(self, feed):
        self.feed = feed
        self.lock = RLock()
        self.cursor = 0
        self.clock = 0
        self.customers = []
        self.sites = []
        self.offers = {}
        self.grants = {}
        self.orders = {}
        self.artifacts = {}
        self.versions = {}
        self.plan_history = {}
        self.delegates = {}
        self.deliveries = {}
        self.rights = {}
        self.log = []

    # ---------- 台账 ----------

    def load_register(self):
        status, data = self.feed.get("/register")
        if status != 200:
            raise RuntimeError(f"台账读取失败：{status}")
        self.load_register_data(data)

    def load_register_data(self, data):
        with self.lock:
            self.customers = list(data["customers"])
            self.sites = list(data["sites"])
            self.offers = dict(data["offers"])
            self.grants = {}
            for grant in data["grants"]:
                self.grants[grant["source"]] = {
                    "scope": grant["scope"],
                    "purpose": list(grant["purpose"]),
                    "sites": list(grant.get("sites", self.sites)),
                    "new_issuance": True,
                    "existing_retention": bool(grant.get("existing_retention", False)),
                    "valid_from": grant.get("valid_from"),
                    "valid_to": grant.get("valid_to"),
                    "narrowed_at": None,
                }
            self.orders = {}
            self.plan_history = {}
            for row in data["orders"]:
                self.orders[row["id"]] = {
                    "id": row["id"],
                    "buyer": row["buyer"],
                    "requested": list(row["sources"]),
                    "sources": list(row["sources"]),
                    "purpose": row["purpose"],
                    "delegate": row.get("delegate"),
                    "state": "registered",
                    "units": 0,
                    "history": [],
                }
                self.plan_history.setdefault(row["buyer"], [(None, row.get("plan", "standard"))])
            self.artifacts = dict(data["artifacts"])
            self.versions = {}
            for name, artifact in self.artifacts.items():
                payload = json.dumps(artifact, ensure_ascii=False, sort_keys=True)
                self.versions[name] = sha256(payload.encode("utf-8")).hexdigest()[:12]

    # ---------- 到达记录办理 ----------

    def sync(self):
        """拉取到达记录并办理到当前，随后核对未决回执。"""
        while True:
            status, page = self.feed.get("/intake", {"since": self.cursor, "limit": 100})
            if status != 200:
                raise RuntimeError(f"到达记录读取失败：{status}")
            items = page["items"]
            with self.lock:
                for row in items:
                    self.apply(row)
                self.cursor = page["cursor"]
            if len(items) < 100:
                break
        self.reconcile()
        return self.cursor

    def apply(self, row):
        with self.lock:
            at = row["at"]
            facts = row["facts"]
            self.clock = max(self.clock, at)
            source = row["source"]
            if source == "request":
                self.apply_request(facts, at)
            elif source == "client":
                if "plan" in facts:
                    self.apply_plan(facts, at)
                if "delegate" in facts:
                    self.apply_delegate(facts, at)
            elif source == "supplier":
                self.apply_supplier(facts, at)
            elif source == "dispatch":
                self.apply_dispatch(facts, at)
            self.log.append({"sequence": row.get("sequence"), "at": at,
                             "source": source, "facts": dict(facts)})

    def apply_request(self, facts, at):
        order = self.orders.get(facts.get("order"))
        if order is None or facts.get("state") != "accepted":
            return
        units = int(facts.get("units", 0))
        checks = self.evaluate(order["sources"], order["purpose"], order["delegate"], at)
        refused = [c for c in checks if not c["allowed"]]
        if refused:
            order["state"] = "blocked"
            order["history"].append({"at": at, "event": "request_refused",
                                     "reasons": {c["source"]: c["reasons"] for c in refused}})
            return
        quota = self.quota(order["buyer"], at)
        if quota["occupied"] + quota["consumed"] + units > quota["total"]:
            order["state"] = "blocked"
            order["history"].append({"at": at, "event": "request_refused",
                                     "reasons": {"quota": ["quota_exceeded"]}})
            return
        if len(self.in_flight(order["buyer"])) >= quota["parallel"]:
            order["state"] = "blocked"
            order["history"].append({"at": at, "event": "request_refused",
                                     "reasons": {"quota": ["parallel_limit"]}})
            return
        order["state"] = "accepted"
        order["units"] = units
        order["history"].append({"at": at, "event": "accepted", "units": units, "checks": checks})

    def apply_plan(self, facts, at):
        plan = facts["plan"]
        if plan not in self.offers:
            return
        effective = facts.get("effective", at)
        buyers = [facts["buyer"]] if facts.get("buyer") else list(self.customers)
        for buyer in buyers:
            history = self.plan_history.setdefault(buyer, [])
            history.append((effective, plan))
            history.sort(key=lambda item: (item[0] is not None, item[0]))

    def apply_supplier(self, facts, at):
        grant = self.grants.get(facts.get("source"))
        if grant is None:
            return
        effective = facts.get("effective", at)
        if "new_issuance" in facts:
            grant["new_issuance"] = bool(facts["new_issuance"])
        if "existing_retention" in facts:
            grant["existing_retention"] = bool(facts["existing_retention"])
        if not grant["new_issuance"] and grant["narrowed_at"] is None:
            grant["narrowed_at"] = effective
        for order in self.orders.values():
            if order["state"] in ("registered", "accepted") and facts["source"] in order["sources"]:
                order["history"].append({"at": at, "event": "grant_narrowed",
                                         "source": facts["source"]})

    def apply_delegate(self, facts, at):
        name = facts.get("delegate")
        if not name:
            return
        entry = self.delegates.setdefault(name, {"withdrawn": False, "withdrawn_at": None})
        if facts.get("withdrawn"):
            entry["withdrawn"] = True
            entry["withdrawn_at"] = at

    def apply_dispatch(self, facts, at):
        order = self.orders.get(facts.get("order"))
        artifact = facts.get("artifact")
        if order is None or artifact not in self.artifacts:
            return
        key = (order["id"], artifact)
        if key in self.deliveries:
            return  # 同一实际交付只记一次，不在两个档位各记一笔
        self.deliveries[key] = {
            "order": order["id"],
            "artifact": artifact,
            "sent_at": facts.get("sent_at", at),
            "recipient": None,
            "status": "unverified",
            "version": self.versions[artifact],
        }
        if order["state"] in ("registered", "accepted", "blocked"):
            order["state"] = "delivered"
        order["history"].append({"at": at, "event": "dispatched",
                                 "artifact": artifact, "sent_at": facts.get("sent_at", at)})

    # ---------- 回执核对 ----------

    def reconcile(self):
        """核对未决交付的回执，并刷新供应方权利确认。"""
        with self.lock:
            pending = [dict(d) for d in self.deliveries.values() if d["status"] == "unverified"]
            sources = [s for s in self.grants if s not in self.rights]
        for probe in pending:
            status, body = self.feed.get("/receipt", {
                "action": "delivery", "desk": "dispatch", "ref": probe["order"]})
            if status != 200:
                continue  # 回执未核实：交付继续占用额度
            evidence = body.get("evidence", {})
            with self.lock:
                delivery = self.deliveries.get((probe["order"], probe["artifact"]))
                if delivery is None or delivery["status"] != "unverified":
                    continue
                delivery["recipient"] = evidence.get("recipient")
                if evidence.get("sent_at") is not None:
                    delivery["sent_at"] = evidence["sent_at"]
                matched = evidence.get("artifact", probe["artifact"]) == probe["artifact"]
                delivery["status"] = "verified" if matched else "conflict"
                order = self.orders[delivery["order"]]
                if matched:
                    order["state"] = "verified"
                order["history"].append({"at": self.clock, "event": "delivery_reconciled",
                                         "artifact": delivery["artifact"], "evidence": evidence})
        for source in sources:
            status, body = self.feed.get("/receipt", {
                "action": "rights", "desk": "supplier", "ref": source})
            if status == 200:
                with self.lock:
                    self.rights[source] = dict(body.get("evidence", {}))

    # ---------- 许可核对 ----------

    def evaluate(self, sources, purpose, delegate, at):
        return [self.evaluate_source(source, purpose, delegate, at) for source in sources]

    def evaluate_source(self, source, purpose, delegate, at):
        """核对一个来源允许的用途、粒度、厂区与有效期。"""
        with self.lock:
            grant = self.grants.get(source)
            if grant is None:
                return {"source": source, "allowed": False,
                        "reasons": ["unknown_source"], "confirmed": False}
            confirmed = self.rights.get(source)
            purposes = confirmed.get("purpose", grant["purpose"]) if confirmed else grant["purpose"]
            scope = confirmed.get("scope", grant["scope"]) if confirmed else grant["scope"]
            reasons = []
            if purpose not in purposes:
                reasons.append("purpose_not_allowed")
            if delegate and purpose == SELF_PURPOSE:
                reasons.append("delegate_reading_internal_only")
            for artifact in self.artifacts.values():
                if artifact["source"] != source:
                    continue
                if GRAIN_LEVEL.get(artifact["grain"], "detail") == "detail" and scope != "detail":
                    reasons.append("grain_exceeds_scope")
                if artifact["site"] not in grant["sites"]:
                    reasons.append("site_not_covered")
            if not grant["new_issuance"]:
                reasons.append("new_issuance_closed")
            if grant["valid_from"] is not None and at is not None and at < grant["valid_from"]:
                reasons.append("not_yet_valid")
            if grant["valid_to"] is not None and at is not None and at > grant["valid_to"]:
                reasons.append("expired")
            unique = list(dict.fromkeys(reasons))
            return {"source": source, "allowed": not unique, "reasons": unique,
                    "confirmed": confirmed is not None}

    # ---------- 额度 ----------

    def plan_at(self, buyer, at):
        plan = None
        for effective, name in self.plan_history.get(buyer, []):
            if effective is None or effective <= at:
                plan = name
        return plan

    def quota(self, buyer, at=None):
        """额度视图：在办占用、已兑现与可余量。档位变更只改总量，占用延续。"""
        with self.lock:
            at = self.clock if at is None else at
            plan = self.plan_at(buyer, at)
            offer = self.offers.get(plan) or {"units": 0, "parallel": 0}
            occupied = consumed = 0
            for order in self.orders.values():
                if order["buyer"] != buyer:
                    continue
                if order["state"] in ("accepted", "delivered"):
                    occupied += order["units"]
                elif order["state"] == "verified":
                    consumed += order["units"]
            return {
                "plan": plan,
                "total": offer["units"],
                "parallel": offer["parallel"],
                "occupied": occupied,
                "consumed": consumed,
                "available": max(offer["units"] - occupied - consumed, 0),
            }

    def in_flight(self, buyer):
        return [order["id"] for order in self.orders.values()
                if order["buyer"] == buyer and order["state"] in ("accepted", "delivered")]

    # ---------- 经办办理 ----------

    def cancel(self, order_id, at=None):
        """撤销未交付请求并释放占用额度；已交付的不可撤销。"""
        with self.lock:
            order = self.orders.get(order_id)
            if order is None:
                return 404, {"error": "order_not_found"}
            if any(d["order"] == order_id for d in self.deliveries.values()):
                return 409, {"error": "already_delivered"}
            if order["state"] not in ("registered", "accepted", "blocked"):
                return 409, {"error": "not_cancellable", "state": order["state"]}
            released = order["units"]
            order["units"] = 0
            order["state"] = "cancelled"
            order["history"].append({"at": self.clock if at is None else at,
                                     "event": "cancelled", "released": released})
            return 200, {"order": order_id, "state": "cancelled", "released": released}

    def reduce(self, order_id, sources=None, purpose=None, delegate=KEEP,
               units=None, confirm_insufficient=False, at=None):
        """受理合规的缩减请求；范围不足时须先确认用户愿意接受。"""
        with self.lock:
            order = self.orders.get(order_id)
            if order is None:
                return 404, {"error": "order_not_found"}
            if any(d["order"] == order_id for d in self.deliveries.values()):
                return 409, {"error": "already_delivered"}
            at = self.clock if at is None else at
            new_sources = list(order["sources"] if sources is None else sources)
            new_purpose = order["purpose"] if purpose is None else purpose
            new_delegate = order["delegate"] if delegate is KEEP else delegate
            checks = self.evaluate(new_sources, new_purpose, new_delegate, at)
            allowed = [c["source"] for c in checks if c["allowed"]]
            insufficient = [{"source": c["source"], "reasons": c["reasons"]}
                            for c in checks if not c["allowed"]]
            if not allowed:
                return 422, {"error": "no_compliant_scope", "insufficient": insufficient}
            if insufficient and not confirm_insufficient:
                return 409, {"error": "confirmation_required",
                             "acceptable": allowed, "insufficient": insufficient}
            new_units = int(units) if units is not None else (order["units"] or 1)
            quota = self.quota(order["buyer"], at)
            if quota["occupied"] + quota["consumed"] - order["units"] + new_units > quota["total"]:
                return 409, {"error": "quota_exceeded", "quota": quota}
            dropped = [s for s in order["sources"] if s not in allowed]
            order["sources"] = allowed
            order["purpose"] = new_purpose
            order["delegate"] = new_delegate
            order["units"] = new_units
            order["state"] = "accepted"
            order["history"].append({"at": at, "event": "reduced", "sources": allowed,
                                     "dropped": dropped, "insufficient": insufficient,
                                     "confirmed": bool(confirm_insufficient),
                                     "units": new_units})
            return 200, {"order": order_id, "state": "accepted", "sources": allowed,
                         "dropped": dropped, "units": new_units}

    # ---------- 查询 ----------

    def delegate_fetch(self, name, order_id):
        """受托方取包：退出后旧查询不能再取，许可被收回的包不再放行。"""
        with self.lock:
            order = self.orders.get(order_id)
            if order is None:
                return 404, {"error": "order_not_found"}
            if order["delegate"] != name:
                return 403, {"error": "not_entrusted"}
            delegate = self.delegates.get(name) or {}
            if delegate.get("withdrawn"):
                return 403, {"error": "delegate_withdrawn",
                             "withdrawn_at": delegate.get("withdrawn_at")}
            packages = []
            for delivery in self.deliveries.values():
                if delivery["order"] != order_id:
                    continue
                artifact = self.artifacts[delivery["artifact"]]
                grant = self.grants[artifact["source"]]
                if not grant["new_issuance"] and not grant["existing_retention"]:
                    return 403, {"error": "retention_withdrawn", "source": artifact["source"]}
                packages.append({
                    "artifact": delivery["artifact"],
                    "version": delivery["version"],
                    "site": artifact["site"],
                    "month": artifact["month"],
                    "grain": artifact["grain"],
                    "values": artifact["values"],
                    "sent_at": delivery["sent_at"],
                    "status": delivery["status"],
                })
            if not packages:
                return 404, {"error": "no_package"}
            return 200, {"order": order_id, "delegate": name, "packages": packages}

    def factory_account(self, buyer):
        """工厂视图：可继续领取的报告、未兑现的额度和收费依据。"""
        with self.lock:
            if buyer not in self.customers:
                return None
            receivable = []
            unfulfilled = []
            for order in self.orders.values():
                if order["buyer"] != buyer:
                    continue
                sent = [d for d in self.deliveries.values() if d["order"] == order["id"]]
                if sent:
                    for delivery in sent:
                        artifact = self.artifacts[delivery["artifact"]]
                        receivable.append({
                            "order": order["id"],
                            "artifact": delivery["artifact"],
                            "version": delivery["version"],
                            "site": artifact["site"],
                            "month": artifact["month"],
                            "sent_at": delivery["sent_at"],
                            "recipient": delivery["recipient"],
                            "status": delivery["status"],
                        })
                elif order["state"] != "cancelled":
                    checks = self.evaluate(order["sources"], order["purpose"],
                                           order["delegate"], self.clock)
                    reasons = {c["source"]: c["reasons"] for c in checks if not c["allowed"]}
                    unfulfilled.append({"order": order["id"], "state": order["state"],
                                        "requested": order["requested"], "reasons": reasons})
            quota = self.quota(buyer)
            billing = {
                "plan": quota["plan"],
                "plan_history": [{"effective": e, "plan": p}
                                 for e, p in self.plan_history.get(buyer, [])],
                "units_total": quota["total"],
                "units_occupied": quota["occupied"],
                "units_consumed": quota["consumed"],
                "units_available": quota["available"],
            }
            return {"buyer": buyer, "receivable": receivable, "unfulfilled": unfulfilled,
                    "quota": quota, "billing": billing}

    def supplier_usage(self, source):
        """供应方视图：自己的授权被用于哪些交付，以及当前许可状态。"""
        with self.lock:
            grant = self.grants.get(source)
            if grant is None:
                return None
            deliveries = []
            for delivery in self.deliveries.values():
                artifact = self.artifacts[delivery["artifact"]]
                if artifact["source"] != source:
                    continue
                deliveries.append({
                    "order": delivery["order"],
                    "buyer": self.orders[delivery["order"]]["buyer"],
                    "artifact": delivery["artifact"],
                    "version": delivery["version"],
                    "recipient": delivery["recipient"],
                    "sent_at": delivery["sent_at"],
                    "status": delivery["status"],
                })
            return {"source": source, "grant": dict(grant),
                    "rights": self.rights.get(source), "deliveries": deliveries}

    def order_detail(self, order_id):
        with self.lock:
            order = self.orders.get(order_id)
            if order is None:
                return None
            detail = dict(order)
            detail["evaluation"] = self.evaluate(order["sources"], order["purpose"],
                                                 order["delegate"], self.clock)
            detail["deliveries"] = [dict(d) for d in self.deliveries.values()
                                    if d["order"] == order_id]
            if order["delegate"]:
                detail["delegate_state"] = self.delegates.get(order["delegate"])
            return detail

    def delivery_record(self, order_id):
        """实发内容与版本：发给了谁、用了哪个版本、回执是否核实。"""
        with self.lock:
            rows = []
            for delivery in self.deliveries.values():
                if delivery["order"] != order_id:
                    continue
                artifact = self.artifacts[delivery["artifact"]]
                rows.append({**delivery, "site": artifact["site"], "month": artifact["month"],
                             "source": artifact["source"], "grain": artifact["grain"],
                             "values": artifact["values"]})
            return rows or None

    def overview(self):
        with self.lock:
            return {
                "cursor": self.cursor,
                "clock": self.clock,
                "quota": {buyer: self.quota(buyer) for buyer in self.customers},
                "orders": {oid: {"buyer": o["buyer"], "state": o["state"], "units": o["units"]}
                           for oid, o in self.orders.items()},
                "deliveries": [dict(d) for d in self.deliveries.values()],
                "delegates": {k: dict(v) for k, v in self.delegates.items()},
            }


class DeskApi(ThreadingHTTPServer):
    def __init__(self, port, desk):
        self.desk = desk
        super().__init__(("127.0.0.1", port), DeskHandler)


class DeskHandler(BaseHTTPRequestHandler):
    def respond(self, status, data):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        desk = self.server.desk
        if len(parts) == 3 and parts[0] == "factory" and parts[2] == "account":
            account = desk.factory_account(parts[1])
            return self.respond(200, account) if account else self.respond(
                404, {"error": "buyer_not_found"})
        if len(parts) == 3 and parts[0] == "supplier" and parts[2] == "usage":
            usage = desk.supplier_usage(parts[1])
            return self.respond(200, usage) if usage else self.respond(
                404, {"error": "source_not_found"})
        if len(parts) == 3 and parts[0] == "operator" and parts[1] == "orders":
            detail = desk.order_detail(parts[2])
            return self.respond(200, detail) if detail else self.respond(
                404, {"error": "order_not_found"})
        if parts == ["operator", "overview"]:
            return self.respond(200, desk.overview())
        if len(parts) == 2 and parts[0] == "deliveries":
            record = desk.delivery_record(parts[1])
            return self.respond(200, {"order": parts[1], "deliveries": record}) \
                if record else self.respond(404, {"error": "delivery_not_found"})
        if len(parts) == 4 and parts[0] == "delegate" and parts[2] == "packages":
            status, body = desk.delegate_fetch(parts[1], parts[3])
            return self.respond(status, body)
        self.respond(404, {"error": "not_found"})

    def do_POST(self):
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        desk = self.server.desk
        if parts == ["sync"]:
            return self.respond(200, {"cursor": desk.sync()})
        body = self.read_body()
        if len(parts) == 4 and parts[0] == "operator" and parts[1] == "orders" \
                and parts[3] == "cancel":
            status, payload = desk.cancel(parts[2])
            return self.respond(status, payload)
        if len(parts) == 4 and parts[0] == "operator" and parts[1] == "orders" \
                and parts[3] == "reduce":
            status, payload = desk.reduce(
                parts[2],
                sources=body.get("sources"),
                purpose=body.get("purpose"),
                delegate=body["delegate"] if "delegate" in body else KEEP,
                units=body.get("units"),
                confirm_insufficient=bool(body.get("confirm_insufficient")),
            )
            return self.respond(status, payload)
        self.respond(404, {"error": "not_found"})

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=8766, type=int)
    parser.add_argument("--feed", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    desk = Desk(FeedClient.from_url(args.feed))
    desk.load_register()
    desk.sync()
    with DeskApi(args.port, desk) as service:
        print(f"经办后端 http://127.0.0.1:{service.server_port}", flush=True)
        service.serve_forever()


if __name__ == "__main__":
    main()
