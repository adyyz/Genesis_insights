"""Versioned synthetic corpus. Reference annotations never become analyst feedback."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
import hashlib
import os
from pathlib import Path
import random
import sqlite3

from .dataset import content_hash, stable_id
from .engine import RULES, auto_label, detect, enrich, prioritize, risk_assessment
from .models import Account, Case, Customer, Dataset, Split, Transaction

ID = "synthetic-bulk-v1-20261018"
SEEDED_ID = ID + "-seeded-history"
SEED = 20261018
BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def corpus_root():
    return Path(os.getenv("GENESIS_BULK_DIR", str(ROOT / ".cache" / ID))).resolve()


def identity_data(number):
    """Only 80 transactions are inspected when assembling a case's history."""
    rng = random.Random(SEED + number)
    identity = stable_id(ID, "identity", number)
    account_id = stable_id(ID, "account", number)
    partition = "TRAIN" if number < 200 else "VALIDATION" if number < 250 else "PENDING"
    # Every partition has exactly 60% individuals and 40% organizations.
    kind = "INDIVIDUAL" if number % 5 < 3 else "ORGANIZATION"
    family = number % 12
    customer = Customer(id=identity, dataset_id=ID, display_name=("Morgan Example" if kind == "INDIVIDUAL" else "Cedar Example Pty Ltd") + f" {number + 1:03}",
        customer_type=kind, onboarded_at=BASE - timedelta(days=1000), pep=family == 2,
        synthetic_sanctions_flag=family == 10, deceased_at=BASE - timedelta(days=40) if family == 3 else None,
        profile_context="Fictional synthetic learning corpus; no real identity or analyst verification.")
    account = Account(id=account_id, dataset_id=ID, customer_id=identity, opened_at=BASE - timedelta(days=900),
        prior_activity_at=BASE - timedelta(days=800), history_complete_from=None if family == 11 else BASE - timedelta(days=900))
    transactions, cases = {}, {}
    for index in range(80):
        at = BASE - timedelta(days=89) + timedelta(hours=index * 27 + rng.randrange(12))
        # Dormant identities have one old cluster and a genuinely separated reactivation.
        if family == 4 and index < 79: at -= timedelta(days=500)
        if family == 8: at = BASE - timedelta(days=60) + timedelta(hours=index * 8)
        currency = ("AUD", "USD", "GBP")[index % 3] if family == 5 else "USD" if family == 7 else "AUD"
        tx_kind = "CRYPTO" if family == 1 else "CASH" if family in {6, 8} else "WIRE" if family == 7 else "TRANSFER"
        amount = rng.randint(1_100_000, 3_000_000) if family in {0, 2, 9} else rng.randint(10_000, 700_000)
        if family == 8: amount = rng.randint(810_000, 990_000)
        ident = stable_id(ID, identity, index)
        transactions[ident] = Transaction(id=ident, dataset_id=ID, account_id=account_id, customer_id=identity,
            occurred_at=at, original_minor=amount, currency=currency, amount_aud_minor=amount * (150 if currency == "USD" else 200 if currency == "GBP" else 100) // 100,
            fx_rate_id=f"synthetic-{currency}-v1", direction="CREDIT" if tx_kind == "CASH" else "DEBIT", type=tx_kind,
            counterparty_id=None if family == 11 else f"fictional-counterparty-{index % 8}",
            description="Fictional source transaction; synthetic corpus only.")
        case_id = stable_id(ID, ident, "case")
        cases[case_id] = Case(id=case_id, dataset_id=ID, scenario_family="synthetic_bulk", split=Split.DISCOVERY,
            customer_id=identity, account_ids=[account_id], trigger_transaction_id=ident,
            history_transaction_ids=[], data_quality=[])
    return Dataset(id=ID, seed=SEED, version="1", customers={identity: customer}, accounts={account_id: account},
        transactions=transactions, cases=cases, reference_labels={}, reference_reasons={}, manifest_hash=""), partition


def scenario(case_id, risk):
    """Observable synthetic features and reference draw use independent random streams."""
    rng = random.Random(int(stable_id(case_id, "features"), 16))
    low = rng.random() < (.7 if risk["tier"] == "LOW" else .25)
    return {"primary_typology": "LOW_RISK" if low else "OTHER", "suggested_action": "CLOSE" if rng.random() < .55 else "REVIEW",
        "uncertainty": "HIGH" if rng.random() < .22 else "LOW"}


def generate(root=None):
    root = Path(root or corpus_root())
    if (root / "manifest.json").exists(): return Corpus(root).manifest
    root.mkdir(parents=True, exist_ok=True)
    (root / "identities").mkdir(exist_ok=True)
    index = sqlite3.connect(root / "index.sqlite")
    refs = sqlite3.connect(root / "references.sqlite")
    index.executescript("""CREATE TABLE IF NOT EXISTS cases (ordinal INTEGER PRIMARY KEY, id TEXT UNIQUE, identity_id TEXT, number INTEGER,
        partition TEXT, customer_type TEXT, name TEXT, score REAL, forced INTEGER, risk_tier TEXT, features TEXT, rules TEXT);
        CREATE INDEX IF NOT EXISTS case_scope ON cases(partition, ordinal);
        CREATE INDEX IF NOT EXISTS case_identity ON cases(identity_id, ordinal);
        CREATE TABLE IF NOT EXISTS case_rules (case_id TEXT, rule_id TEXT, PRIMARY KEY(case_id,rule_id));
        CREATE INDEX IF NOT EXISTS rule_lookup ON case_rules(rule_id,case_id);
        DELETE FROM cases; DELETE FROM case_rules;""")
    refs.executescript("CREATE TABLE IF NOT EXISTS annotations (case_id TEXT PRIMARY KEY, partition TEXT, verdict TEXT); DELETE FROM annotations;")
    partitions, counts = [], {rule: 0 for rule in RULES}
    ordinal = 0
    training_metrics = {key: 0 for key in ("TP", "FP", "TN", "FN", "inconclusive")}
    for number in range(500):
        data, partition = identity_data(number)
        identity = next(iter(data.customers.values()))
        payload = {"customer": identity.model_dump(mode="json"), "account": next(iter(data.accounts.values())).model_dump(mode="json"),
            "transactions": [tx.model_dump(mode="json") for tx in data.transactions.values()]}
        digest = content_hash(payload)
        (root / "identities" / f"{identity.id}.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        partitions.append({"identity_id": identity.id, "partition": partition, "hash": digest, "transactions": 80})
        for case in data.cases.values():
            ordinal += 1
            evidence = enrich(data, case)
            risk = risk_assessment(evidence)
            features = scenario(case.id, risk)
            label = auto_label(evidence, features)
            rules = [row["rule_id"] for row in detect(data, case)]
            for rule in rules: counts[rule] += 1
            index.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (ordinal, case.id, identity.id, number, partition,
                identity.customer_type, identity.display_name, label["heuristic_score"], int(label["requires_context_or_protected_review"]),
                risk["tier"], json.dumps(features), json.dumps(rules)))
            index.executemany("INSERT INTO case_rules VALUES (?,?)", [(case.id, rule) for rule in rules])
            if partition != "PENDING":
                # The annotation stream cannot read categorization/heuristic output.
                truth_rng = random.Random(int(stable_id(case.id, "reference"), 16))
                verdict = "INCONCLUSIVE" if evidence["missing_context"] else "BENIGN" if truth_rng.random() < (.7 if risk["tier"] == "LOW" else .35) else "SUSPICIOUS"
                refs.execute("INSERT INTO annotations VALUES (?,?,?)", (case.id, partition, verdict))
                if partition == "TRAIN":
                    key = "inconclusive" if verdict == "INCONCLUSIVE" else ("FP" if rules else "TN") if verdict == "BENIGN" else ("TP" if rules else "FN")
                    training_metrics[key] += 1
    index.commit(); refs.commit()
    eligible = index.execute("SELECT count(*) FROM cases WHERE partition='PENDING' AND rules!='[]'").fetchone()[0]
    index.close(); refs.close()
    body = {"dataset_id": ID, "seed": SEED, "version": 1, "transactions": 40000, "identities": 500, "cases": 40000,
        "reference_annotations": 20000, "training_transactions": 16000, "validation_transactions": 4000, "initially_unprocessed": 20000,
        "eligible_pending_cases": eligible, "rule_alerts": counts, "partitions": partitions,
        "provenance": "SYNTHETIC_REFERENCE_NOT_HUMAN_REVIEW", "training_detector_metrics": training_metrics,
        "index_sha256": hashlib.sha256((root / "index.sqlite").read_bytes()).hexdigest(),
        "references_sha256": hashlib.sha256((root / "references.sqlite").read_bytes()).hexdigest()}
    manifest = {**body, "manifest_hash": content_hash(body)}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


class Corpus:
    def __init__(self, root=None):
        self.root = Path(root or corpus_root())
        if not (self.root / "manifest.json").exists(): raise LookupError("Generate the synthetic learning corpus locally first")
        self.manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        body = {k: v for k, v in self.manifest.items() if k != "manifest_hash"}
        if content_hash(body) != self.manifest["manifest_hash"] or body["dataset_id"] != ID: raise ValueError("Corpus manifest integrity failed")

        for name, key in (("index.sqlite", "index_sha256"), ("references.sqlite", "references_sha256")):
            if hashlib.sha256((self.root / name).read_bytes()).hexdigest() != body[key]: raise ValueError("Corpus index/reference integrity failed")

    @contextmanager
    def connect(self, references=False):
        path = self.root / ("references.sqlite" if references else "index.sqlite")
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try: yield db
        finally: db.close()

    def summary(self):
        return {key: value for key, value in self.manifest.items() if key != "partitions"}

    def page(self, *, limit=50, cursor=0, identity="", customer_type="", rule="", query="", partition="PENDING"):
        if not 1 <= limit <= 100 or cursor < 0: raise ValueError("Page size must be 1-100")
        if partition not in {"TRAIN", "PENDING"}: raise ValueError("Reserved validation is not an operational queue")
        clauses, args = ["partition=?"], [partition]
        for column, value in (("identity_id", identity), ("customer_type", customer_type)):
            if value: clauses.append(column + "=?"); args.append(value)
        if rule:
            if rule not in RULES: raise ValueError("Unknown detector rule")
            clauses.append("id IN (SELECT case_id FROM case_rules WHERE rule_id=?)"); args.append(rule)
        if query: clauses.append("name LIKE ?"); args.append("%" + query[:100] + "%")
        where = " AND ".join(clauses)
        with self.connect() as db:
            total = db.execute("SELECT count(*) FROM cases WHERE " + where, args).fetchone()[0]
            rows = db.execute("SELECT * FROM cases WHERE " + where + " AND ordinal>? ORDER BY ordinal LIMIT ?", [*args, cursor, limit + 1]).fetchall()
        return {"items": [self.safe_row(row) for row in rows[:limit]], "total": total,
            "next_cursor": rows[limit - 1]["ordinal"] if len(rows) > limit else None, "manifest_hash": self.manifest["manifest_hash"]}

    @staticmethod
    def safe_row(row):
        return {"id": row["id"], "ordinal": row["ordinal"], "identity_id": row["identity_id"], "name": row["name"], "customer_type": row["customer_type"],
            "partition": row["partition"], "heuristic_score": row["score"], "requires_review": bool(row["forced"]),
            "risk_tier": row["risk_tier"], "rules": json.loads(row["rules"]), "provenance": "DETERMINISTIC_SYNTHETIC_SIMULATION"}

    def row(self, case_id, *, validation=False):
        with self.connect() as db: row = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if row is None or row["partition"] == "VALIDATION" and not validation: raise LookupError("Operational case not found")
        return row

    def dataset(self, case_id):
        row = self.row(case_id)
        data, _ = identity_data(row["number"])
        expected = next(item["hash"] for item in self.manifest["partitions"] if item["identity_id"] == row["identity_id"])
        payload = json.loads((self.root / "identities" / f"{row['identity_id']}.json").read_text(encoding="utf-8"))
        if content_hash(payload) != expected: raise ValueError("Identity partition integrity failed")
        # Load the frozen partition, not newly generated source records.
        data.customers = {payload["customer"]["id"]: Customer.model_validate(payload["customer"])}
        data.accounts = {payload["account"]["id"]: Account.model_validate(payload["account"])}
        data.transactions = {tx["id"]: Transaction.model_validate(tx) for tx in payload["transactions"]}
        data.manifest_hash = self.manifest["manifest_hash"]
        return data

    def case(self, case_id):
        data = self.dataset(case_id)
        case = data.cases[case_id]
        evidence = enrich(data, case); risk = risk_assessment(evidence)
        return {"id": case_id, "name": evidence["customer"]["display_name"], "split": "CALIBRATION_DISCOVERY", "scenario_family": "synthetic_bulk",
            "alerts": detect(data, case), "evidence": evidence, "risk": risk, "ai_status": "NOT_RUN",
            **{"queue_" + key: value for key, value in prioritize(evidence, risk, None).items() if key in {"priority", "reason"}}}

    def observations(self, partition):
        if partition not in {"TRAIN", "VALIDATION"}: raise ValueError("Unknown reference cohort")
        with self.connect() as db, self.connect(True) as refs:
            labels = {row["case_id"]: row["verdict"] for row in refs.execute("SELECT * FROM annotations WHERE partition=?", (partition,))}
            output = []
            for row in db.execute("SELECT * FROM cases WHERE partition=? AND forced=0", (partition,)):
                verdict = {"BENIGN": "FALSE_POSITIVE", "SUSPICIOUS": "TRUE_POSITIVE"}.get(labels.get(row["id"]))
                if verdict not in {"TRUE_POSITIVE", "FALSE_POSITIVE"}: continue
                for rule in json.loads(row["rules"]): output.append({"identity_id": row["identity_id"], "case_id": row["id"], "rule_id": rule,
                    "score": row["score"], "verdict": verdict, "provenance": "SYNTHETIC_REFERENCE"})
            return output


@lru_cache(maxsize=1)
def corpus():
    return Corpus()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = generate(args.output)
    print(json.dumps({k: v for k, v in result.items() if k != "partitions"}))
