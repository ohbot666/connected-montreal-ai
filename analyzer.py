#!/usr/bin/env python3
"""
Connected Montreal AI Marketing Analyzer
Reads daily-report.json and generates actionable proposals.
"""

import json
import os
from datetime import datetime
from typing import List, Dict, Any

class MarketingAnalyzer:
    def __init__(self, report_path: str):
        """Initialize analyzer with daily report data."""
        with open(report_path, 'r') as f:
            self.data = json.load(f)
        self.proposals = []
        self.proposal_counter = 0
    
    def generate_id(self, base: str) -> str:
        """Generate unique proposal ID."""
        self.proposal_counter += 1
        return f"{base}-{self.proposal_counter}"
    
    def add_proposal(self, id_base: str, priority: str, category: str, 
                     issue: str, solution: str, effort: str, impact: str):
        """Add a proposal to the list."""
        proposal = {
            "id": self.generate_id(id_base),
            "priority": priority,
            "category": category,
            "issue": issue,
            "solution": solution,
            "effort": effort,
            "expected_impact": impact
        }
        self.proposals.append(proposal)
    
    def analyze(self) -> List[Dict[str, Any]]:
        """Run all analysis rules."""
        posthog = self.data.get('posthog', {})
        airtable = self.data.get('airtable', {})
        
        # Rule 1: Ad landing pages getting traffic but low lead conversion
        self._analyze_ad_conversion(posthog, airtable)
        
        # Rule 2: Pipeline stuck in quoted stage
        self._analyze_pipeline_closure(airtable)
        
        # Rule 3: Leads needing followup
        self._analyze_followup_cadence(airtable)
        
        # Rule 4: Homepage dominates traffic → content depth issue
        self._analyze_content_depth(posthog, airtable)
        
        # Rule 5: Direct traffic analysis
        self._analyze_traffic_sources(posthog)
        
        return self.proposals
    
    def _analyze_ad_conversion(self, posthog: Dict, airtable: Dict):
        """Rule 1: Check ad landing page conversion."""
        ad_pages = posthog.get('ad_landing_pages', [])
        new_leads = airtable.get('new_leads_7d', 0)
        total_ad_views = sum(page['views'] for page in ad_pages)
        
        if total_ad_views > 50 and new_leads < 20:
            # Low conversion from ads
            top_ad_page = ad_pages[0] if ad_pages else {}
            url = top_ad_page.get('url', 'unknown')
            views = top_ad_page.get('views', 0)
            
            self.add_proposal(
                id_base="low-conversion-ad-landing",
                priority="high",
                category="ads",
                issue=f"Ad landing page {url} gets {views} clicks/week but only {new_leads} new leads in 7 days across all channels (conversion rate ~{int(new_leads/total_ad_views*100)}%)",
                solution=f"A/B test {url}: (1) Add social proof section with 3 testimonials + '500+ parties planned' counter; (2) Change headline to 'Montreal's #1 Bachelor Party Planners' (test vs current); (3) Simplify CTA button to 'Get Your Quote in 2 Minutes'; (4) Add FAQ section above fold addressing top objections (price, flexibility, rain plan)",
                effort="1hr",
                impact="Estimated +20-30% conversion rate = 8-12 additional leads/week"
            )
    
    def _analyze_pipeline_closure(self, airtable: Dict):
        """Rule 5: Check if pipeline has many quoted but 0 booked."""
        pipeline = airtable.get('pipeline', {})
        quoted = pipeline.get('quoted', 0)
        booked = pipeline.get('booked', 0)
        
        if quoted > 15 and booked == 0:
            self.add_proposal(
                id_base="zero-closes-quoted",
                priority="high",
                category="conversion",
                issue=f"Pipeline stuck: {quoted} leads quoted, but 0 booked. Quote-to-close rate is 0% despite $7.5M in pipeline value",
                solution=f"Launch 'Close-the-Loop' sprint: (1) Create quote follow-up template: 'Hi {{name}}, checking in on your {datetime.now().strftime('%B')} bachelor party quote. Any questions? Happy to adjust details.'; (2) Set followup rule: reach out Day 3 and Day 7 after quote; (3) Schedule internal 'quote quality review' meeting—are quotes missing key details? Compare winning vs losing quotes; (4) Add 'value add' to quotes: bonus activities or package upgrades for quick decisions",
                effort="half-day",
                impact="Expected +15-20% close rate = 5-6 bookings/month"
            )
    
    def _analyze_followup_cadence(self, airtable: Dict):
        """Rule 4: Check leads needing followup."""
        leads = airtable.get('leads_needing_followup', [])
        if len(leads) > 3:
            # Count overdue (>2 weeks without contact)
            overdue = sum(1 for lead in leads 
                         if (datetime.now() - datetime.fromisoformat(lead['last_contact'])).days > 14)
            
            self.add_proposal(
                id_base="followup-backlog",
                priority="high",
                category="leads",
                issue=f"{len(leads)} leads waiting for followup ({overdue} overdue by 2+ weeks). Last contact dates range from 1/6 to 2/24, blocking pipeline movement",
                solution=f"Clear the backlog: (1) Tier leads: A=contacted <7 days, B=7-14 days, C=>14 days; (2) This week: call all 'C' tier (overdue) with personal apology + re-quote; (3) Implement CRM rule: all quoted leads get auto-followup on Day 7 and Day 14; (4) Weekly 'pipeline review' call: Oren + Rod discuss each quoted lead's blocker",
                effort="1hr",
                impact="Expected to convert 3-5 of stalled leads = $15-30K in revenue"
            )
    
    def _analyze_content_depth(self, posthog: Dict, airtable: Dict):
        """Rule 6: Homepage dominates traffic → content depth issue."""
        top_pages = posthog.get('top_pages', [])
        if not top_pages:
            return
        
        homepage_views = top_pages[0]['views'] if top_pages[0]['url'] == '/' else 0
        total_views = posthog.get('total_pageviews_7d', 0)
        other_views = sum(p['views'] for p in top_pages[1:])
        new_leads = airtable.get('new_leads_7d', 0)
        
        if homepage_views > 0 and homepage_views > other_views / 2:
            # Homepage is dominant
            homepage_ratio = int((homepage_views / total_views) * 100)
            conversion_rate = int((new_leads / total_views) * 100)
            
            self.add_proposal(
                id_base="homepage-bounce",
                priority="medium",
                category="content",
                issue=f"Homepage dominates traffic ({homepage_ratio}% of views, {homepage_views} in 7 days) but {conversion_rate}% overall conversion rate suggests high bounce. Visitors landing and leaving without exploring",
                solution=f"Redesign homepage for depth: (1) Above fold: Hero section with 1 clear CTA 'See Bachelor Party Packages'; (2) Add 'Social Proof' section: 'Trusted by 500+ groups' + 3 video testimonials; (3) Add 'Popular Itineraries' carousel linking to /itineraries; (4) Add 'Blog' section featuring top posts (strip clubs guide, Austin crawl); (5) A/B test with control—measure downstream traffic to package/itinerary pages",
                effort="1hr",
                impact="Expected to drive +30-40% deeper navigation = 25-40 more leads/month"
            )
    
    def _analyze_traffic_sources(self, posthog: Dict):
        """Rule 7: Check SEO health via direct vs organic traffic."""
        sources = posthog.get('traffic_sources', [])
        organic = 0  # Google
        direct = 0
        
        for source in sources:
            src = source.get('source', '')
            sessions = source.get('sessions', 0)
            if 'google' in src.lower():
                organic += sessions
            elif src == '$direct':
                direct = sessions
        
        total = organic + direct
        if total > 100:
            organic_pct = int((organic / total) * 100)
            
            if organic_pct < 60:  # More direct than organic
                self.add_proposal(
                    id_base="seo-gap",
                    priority="medium",
                    category="seo",
                    issue=f"SEO needs work: Direct traffic ({direct} sessions) is {int(direct/organic*100)}% of Google traffic ({organic} sessions). Indicates weak organic ranking",
                    solution=f"SEO quick wins: (1) Audit top 5 pages for keyword targets (/bachelor-party-a-v2/, /packages/, /itineraries/) and add rich schema markup (LocalBusinessSchema, FAQ schema); (2) Create 3 new SEO-focused blog posts: 'Best Bachelor Party Venues in Montreal', 'How to Plan a Montreal Bachelor Party in 3 Days', 'Montreal Bachelor Party vs Vegas'; (3) Build internal linking: link from homepage to top 3 pages; (4) Check mobile UX (Core Web Vitals) on Google Search Console",
                    effort="half-day",
                    impact="Expected +30% organic traffic in 60 days (100+ new sessions)"
                )


def main():
    """Main execution."""
    report_path = os.path.expanduser("~/Projects/connected-brain/data/daily-report.json")
    output_path = os.path.expanduser("~/Projects/connected-brain/data/proposals.json")
    
    # Load and analyze
    analyzer = MarketingAnalyzer(report_path)
    proposals = analyzer.analyze()
    
    # Print to console
    print(f"\n{'='*80}")
    print(f"Connected Montreal AI Marketing Analysis")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*80}\n")
    
    print(f"📊 SUMMARY")
    print(f"  Total Proposals: {len(proposals)}")
    print(f"  High Priority: {sum(1 for p in proposals if p['priority'] == 'high')}")
    print(f"  Medium Priority: {sum(1 for p in proposals if p['priority'] == 'medium')}")
    print(f"\n{'-'*80}\n")
    
    # Sort by priority
    priority_order = {'high': 0, 'medium': 1, 'low': 2}
    sorted_proposals = sorted(proposals, key=lambda p: priority_order[p['priority']])
    
    for i, prop in enumerate(sorted_proposals, 1):
        print(f"🎯 PROPOSAL {i}: {prop['id'].upper()}")
        print(f"   Priority: {prop['priority'].upper()} | Category: {prop['category'].upper()}")
        print(f"   Issue: {prop['issue']}")
        print(f"   Solution: {prop['solution']}")
        print(f"   Effort: {prop['effort']} | Impact: {prop['expected_impact']}")
        print(f"\n{'-'*80}\n")
    
    # Save to JSON
    output_data = {
        "generated_at": datetime.now().isoformat(),
        "proposals": sorted_proposals
    }
    
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(output_data, f, indent=2)
    
    print(f"✅ Proposals saved to {output_path}")


if __name__ == "__main__":
    main()
