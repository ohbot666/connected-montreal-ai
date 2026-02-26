#!/usr/bin/env python3
"""
Connected Montreal - AI Marketing Data Collector
Pulls data from PostHog and Airtable, generates daily report
"""

import requests
import json
import os
from datetime import datetime, timedelta, timezone
from collections import Counter
from pathlib import Path


class DataCollector:
    def __init__(self):
        self.posthog_api_key = "phx_10r8mxfGxYI4gU863o057kfjjHrUPsiwpOipfPofxCRBV77P"
        self.posthog_host = "https://us.posthog.com"
        self.posthog_project = 259946  # connectedmontreal.com

        airtable_token_file = Path("/Users/orenborn/.openclaw/workspaces/connected-montreal/.airtable-token")
        self.airtable_token = airtable_token_file.read_text().strip() if airtable_token_file.exists() else None
        self.base_id = "appHT9Re4l53GO16t"
        self.customers_table = "tbl4P7tqdonXv5vcY"

        self.end_date = datetime.now(timezone.utc)
        self.start_date = self.end_date - timedelta(days=7)
        self.output_path = Path(os.path.expanduser("~/Projects/connected-brain/data/daily-report.json"))

    def get_posthog_data(self):
        headers = {"Authorization": f"Bearer {self.posthog_api_key}"}
        result = {"top_pages": [], "traffic_sources": [], "total_pageviews_7d": 0, "avg_daily_pageviews": 0, "ad_landing_pages": []}

        try:
            all_events = []
            url = f"{self.posthog_host}/api/projects/{self.posthog_project}/events/"
            params = {"event": "$pageview", "limit": 1000, "after": self.start_date.strftime("%Y-%m-%dT%H:%M:%S")}

            while url:
                r = requests.get(url, headers=headers, params=params, timeout=15)
                if r.status_code != 200:
                    break
                data = r.json()
                all_events.extend(data.get("results", []))
                url = data.get("next")
                params = {}  # next URL already has params

            result["total_pageviews_7d"] = len(all_events)
            result["avg_daily_pageviews"] = round(len(all_events) / 7, 1)

            # Top pages by pathname
            pages = Counter(e.get("properties", {}).get("$pathname", "/") for e in all_events)
            result["top_pages"] = [{"url": url, "views": count} for url, count in pages.most_common(10)]

            # Traffic sources
            sources = Counter(
                e.get("properties", {}).get("$utm_source") or e.get("properties", {}).get("$referring_domain") or "direct"
                for e in all_events
            )
            result["traffic_sources"] = [{"source": src, "sessions": cnt} for src, cnt in sources.most_common(8)]

            # Ad landing pages (have gclid or utm_source=google)
            ad_events = [e for e in all_events if e.get("properties", {}).get("gclid") or e.get("properties", {}).get("$utm_source") == "google"]
            ad_pages = Counter(e.get("properties", {}).get("$pathname", "/") for e in ad_events)
            result["ad_landing_pages"] = [{"url": url, "views": count} for url, count in ad_pages.most_common(5)]

        except Exception as e:
            print(f"❌ PostHog error: {e}")

        return result

    def get_airtable_data(self):
        result = {"new_leads_7d": 0, "pipeline": {"new": 0, "quoted": 0, "booked": 0, "no_go": 0}, "leads_needing_followup": [], "total_pipeline_value": 0}
        if not self.airtable_token:
            return result

        headers = {"Authorization": f"Bearer {self.airtable_token}"}
        records = []
        url = f"https://api.airtable.com/v0/{self.base_id}/{self.customers_table}"
        offset = None

        try:
            while True:
                params = {"pageSize": 100}
                if offset:
                    params["offset"] = offset
                r = requests.get(url, headers=headers, params=params, timeout=15)
                if r.status_code != 200:
                    print(f"⚠️  Airtable error {r.status_code}")
                    break
                data = r.json()
                records.extend(data.get("records", []))
                offset = data.get("offset")
                if not offset:
                    break

            status_map = {"New Request": "new", "talked to/ quoted": "quoted", "Booked": "booked", "No Go": "no_go"}

            for rec in records:
                fields = rec.get("fields", {})
                status = fields.get("Status", "")
                created = rec.get("createdTime", "")

                # New leads in last 7 days
                try:
                    created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    if created_dt >= self.start_date:
                        result["new_leads_7d"] += 1
                except:
                    pass

                # Pipeline counts
                bucket = status_map.get(status)
                if bucket:
                    result["pipeline"][bucket] += 1

                # Pipeline value (Grand Total field)
                if status not in ["No Go", ""]:
                    val = fields.get("Grand Total") or fields.get("Service Total") or 0
                    try:
                        result["total_pipeline_value"] += float(str(val).replace(",", "").replace("$", "")) if val else 0
                    except:
                        pass

                # Leads needing followup (New or Quoted)
                if status in ["New Request", "talked to/ quoted"]:
                    name = fields.get("Name", "Unknown")
                    followup_date = fields.get("Status Update Date", fields.get("First Contact Date", "Unknown"))
                    result["leads_needing_followup"].append({
                        "name": name, "status": status, "last_contact": str(followup_date)
                    })

            result["leads_needing_followup"] = result["leads_needing_followup"][:10]
            result["total_pipeline_value"] = round(result["total_pipeline_value"], 2)

        except Exception as e:
            print(f"❌ Airtable error: {e}")

        return result

    def generate_insights(self, ph, at):
        issues, opps = [], []

        if ph["total_pageviews_7d"] < 100:
            issues.append(f"Low traffic: only {ph['total_pageviews_7d']} pageviews in 7 days")
        if at["new_leads_7d"] == 0:
            issues.append("No new leads in the past 7 days")
        elif at["pipeline"]["quoted"] == 0 and at["new_leads_7d"] > 0:
            issues.append(f"{at['new_leads_7d']} new leads but none have been quoted yet")

        if ph["ad_landing_pages"]:
            top_ad = ph["ad_landing_pages"][0]
            opps.append(f"Top ad landing page: {top_ad['url']} ({top_ad['views']} ad clicks)")
        if at["pipeline"]["quoted"] > 0:
            opps.append(f"{at['pipeline']['quoted']} leads currently in quoted stage — close them")
        if at["total_pipeline_value"] > 0:
            opps.append(f"${at['total_pipeline_value']:,.0f} in active pipeline value")

        return {
            "issues": issues or ["No critical issues detected"],
            "opportunities": opps or ["Keep monitoring — more data needed"]
        }

    def run(self):
        print("🚀 Connected Montreal Data Collector")
        print(f"   Period: {self.start_date.date()} → {self.end_date.date()}\n")

        print("📊 Fetching PostHog...")
        ph = self.get_posthog_data()
        print(f"   ✅ {ph['total_pageviews_7d']} pageviews, {len(ph['top_pages'])} pages tracked")

        print("📋 Fetching Airtable...")
        at = self.get_airtable_data()
        print(f"   ✅ {sum(at['pipeline'].values())} total leads in pipeline")

        insights = self.generate_insights(ph, at)

        report = {
            "generated_at": datetime.now().isoformat(),
            "period_days": 7,
            "posthog": ph,
            "airtable": at,
            "insights": insights
        }

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.output_path, "w") as f:
            json.dump(report, f, indent=2)

        print(f"\n✅ Saved → {self.output_path}")
        print("\n" + "="*50)
        print("SUMMARY")
        print("="*50)
        print(f"🌐 Traffic (7d): {ph['total_pageviews_7d']} pageviews, ~{ph['avg_daily_pageviews']}/day")
        if ph["top_pages"]:
            print("   Top pages:")
            for p in ph["top_pages"][:5]:
                print(f"   • {p['url']} — {p['views']} views")
        print(f"\n👥 Pipeline: {at['new_leads_7d']} new | {at['pipeline']['quoted']} quoted | {at['pipeline']['booked']} booked | ${at['total_pipeline_value']:,.0f} value")
        print(f"\n⚠️  Issues:")
        for i in insights["issues"]:
            print(f"   • {i}")
        print(f"\n💡 Opportunities:")
        for o in insights["opportunities"]:
            print(f"   • {o}")


if __name__ == "__main__":
    collector = DataCollector()
    collector.run()
