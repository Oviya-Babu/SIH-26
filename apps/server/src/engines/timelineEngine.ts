// ─── Timeline Engine ─────────────────────────────────────────────────────────
// PRD Section 11: Merges patient-reported + document-extracted facts into
// a longitudinal timeline. Unknown dates shown as-is, never inferred.

import type { ClinicalFact, TimelineEvent } from '@medikiosk/shared';
import { v4 as uuid } from 'uuid';

class TimelineEngine {
  /**
   * Build timeline events from clinical facts.
   * Each fact becomes a timeline event if it has temporal or historical relevance.
   */
  buildTimeline(facts: ClinicalFact[], sessionId: string): TimelineEvent[] {
    const events: TimelineEvent[] = [];

    for (const fact of facts) {
      const event = this.factToTimelineEvent(fact, sessionId);
      if (event) events.push(event);
    }

    // Sort: known dates (newest first), then approximate, then unknown
    return events.sort((a, b) => {
      if (a.date && b.date) return new Date(b.date).getTime() - new Date(a.date).getTime();
      if (a.date && !b.date) return -1;
      if (!a.date && b.date) return 1;
      return 0;
    });
  }

  private factToTimelineEvent(fact: ClinicalFact, sessionId: string): TimelineEvent | null {
    // Skip facts that don't have temporal/historical meaning
    const temporalCategories = [
      'chief_complaint', 'symptom', 'past_medical_history',
      'past_surgical_history', 'investigation', 'medication'
    ];

    if (!temporalCategories.includes(fact.category)) return null;

    // Extract date information from fact
    const { date, dateApproximate, dateLabel } = this.extractDate(fact);

    // Build readable title and description
    const title = this.buildTitle(fact);
    const description = fact.valueNormalized || fact.valueRaw;

    return {
      id: uuid(),
      sessionId,
      factId: fact.id,
      date,
      dateApproximate,
      dateLabel,
      category: this.formatCategory(fact.category),
      title,
      description,
      source: fact.source === 'document_extracted' ? 'document_extracted' : 'patient_reported',
      provenanceRef: fact.provenanceRef,
      metadata: fact.metadata,
    };
  }

  private extractDate(fact: ClinicalFact): {
    date?: string;
    dateApproximate: boolean;
    dateLabel: string;
  } {
    // Check metadata for document-extracted dates
    if (fact.metadata?.documentDate) {
      return {
        date: fact.metadata.documentDate as string,
        dateApproximate: false,
        dateLabel: new Date(fact.metadata.documentDate as string).toLocaleDateString('en-IN', {
          day: 'numeric', month: 'short', year: 'numeric'
        }),
      };
    }

    // For current visit facts
    if (fact.category === 'chief_complaint' || fact.category === 'symptom') {
      const today = new Date().toISOString().split('T')[0];
      return {
        date: today,
        dateApproximate: false,
        dateLabel: 'Today',
      };
    }

    // For past history — try to extract duration from value
    const durationMatch = fact.valueRaw?.match(/(\d+)\s*(year|month|week|day)/i);
    if (durationMatch) {
      const amount = parseInt(durationMatch[1]);
      const unit = durationMatch[2].toLowerCase();
      const approxDate = new Date();

      switch (unit) {
        case 'year': approxDate.setFullYear(approxDate.getFullYear() - amount); break;
        case 'month': approxDate.setMonth(approxDate.getMonth() - amount); break;
        case 'week': approxDate.setDate(approxDate.getDate() - amount * 7); break;
        case 'day': approxDate.setDate(approxDate.getDate() - amount); break;
      }

      return {
        date: approxDate.toISOString().split('T')[0],
        dateApproximate: true,
        dateLabel: `Approximately ${amount} ${unit}${amount > 1 ? 's' : ''} ago`,
      };
    }

    // Unknown date — never invent one
    return {
      dateApproximate: true,
      dateLabel: 'Unknown / approximate date',
    };
  }

  private buildTitle(fact: ClinicalFact): string {
    switch (fact.category) {
      case 'chief_complaint':
        return `Presenting: ${fact.conceptLabel}`;
      case 'symptom':
        return fact.conceptLabel;
      case 'past_medical_history':
        return `Diagnosed: ${fact.conceptLabel}`;
      case 'past_surgical_history':
        return `Surgery: ${fact.valueNormalized || fact.conceptLabel}`;
      case 'medication':
        return `Medication: ${fact.conceptLabel}`;
      case 'investigation':
        return `Investigation: ${fact.conceptLabel}`;
      default:
        return fact.conceptLabel;
    }
  }

  private formatCategory(category: string): string {
    return category.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
  }
}

export const timelineEngine = new TimelineEngine();
