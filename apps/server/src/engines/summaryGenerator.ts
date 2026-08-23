// ─── Summary Generator (Mock LLM) ───────────────────────────────────────────
// PRD Section 12: Generates structured prose FROM structured facts only.
// In production, this would be an evidence-constrained LLM. For MVP, it's
// template-based generation that simulates the same output structure.

import type { ClinicalFact, ClinicalSummary, SummarySection, MissingInfo, ConflictInfo, RedFlagAlert } from '@medikiosk/shared';
import { v4 as uuid } from 'uuid';
import { conflictDetector } from './conflictDetector.js';

class SummaryGenerator {
  /**
   * Generate a draft clinical summary from structured facts.
   * Every sentence is traceable to source facts — no hallucination by design.
   */
  generate(
    facts: ClinicalFact[],
    redFlagAlerts: RedFlagAlert[],
    sessionId: string,
    patientId: string,
    tenantId: string,
    encounterId: string,
    answeredQuestionIds: string[],
    allRequiredQuestionIds: string[],
  ): ClinicalSummary {
    const sections = this.buildSections(facts);
    const conflicts = conflictDetector.detectConflicts(facts);
    const missingInfo = this.identifyMissing(answeredQuestionIds, allRequiredQuestionIds);

    const requiredAnswered = allRequiredQuestionIds.filter(id =>
      answeredQuestionIds.includes(id)
    ).length;
    const completenessScore = allRequiredQuestionIds.length > 0
      ? requiredAnswered / allRequiredQuestionIds.length : 1;

    return {
      id: uuid(),
      sessionId,
      patientId,
      tenantId,
      encounterId,
      status: 'draft',
      generatedAt: new Date().toISOString(),
      version: 1,
      sections,
      redFlags: redFlagAlerts,
      missingInformation: missingInfo,
      conflicts,
      completenessScore,
    };
  }

  private buildSections(facts: ClinicalFact[]): SummarySection[] {
    const sections: SummarySection[] = [];

    // Chief Complaint
    const ccFacts = facts.filter(f => f.category === 'chief_complaint');
    if (ccFacts.length > 0) {
      sections.push({
        id: uuid(), type: 'chief_complaint', title: 'Chief Complaint',
        content: ccFacts.map(f => f.valueNormalized || f.valueRaw).join('. ') + '.',
        facts: ccFacts.map(f => f.id), reviewStatus: 'pending',
      });
    }

    // HPI
    const hpiFacts = facts.filter(f => f.category === 'symptom');
    if (hpiFacts.length > 0 || ccFacts.length > 0) {
      const hpiContent = this.buildHPIProse([...ccFacts, ...hpiFacts], facts);
      sections.push({
        id: uuid(), type: 'hpi', title: 'History of Present Illness',
        content: hpiContent,
        facts: [...ccFacts, ...hpiFacts].map(f => f.id), reviewStatus: 'pending',
      });
    }

    // Past Medical History
    const pmhFacts = facts.filter(f => f.category === 'past_medical_history');
    sections.push({
      id: uuid(), type: 'past_medical_history', title: 'Past Medical History',
      content: pmhFacts.length > 0
        ? pmhFacts.map(f => {
            const conflict = f.isConflicting ? ' [CONFLICTING]' : '';
            return `• ${f.valueNormalized || f.valueRaw}${conflict}`;
          }).join('\n')
        : 'Not assessed in this session.',
      facts: pmhFacts.map(f => f.id), reviewStatus: 'pending',
    });

    // Medications
    const medFacts = facts.filter(f => f.category === 'medication');
    sections.push({
      id: uuid(), type: 'medications', title: 'Current Medications',
      content: medFacts.length > 0
        ? medFacts.map(f => {
            const source = f.source === 'document_extracted'
              ? ` — extracted from ${(f.metadata?.documentSource as string) || 'document'} (confidence: ${Math.round(f.confidence * 100)}%)`
              : ' — patient-reported';
            return `• ${f.valueNormalized || f.valueRaw}${source}`;
          }).join('\n')
        : 'Not assessed in this session.',
      facts: medFacts.map(f => f.id), reviewStatus: 'pending',
    });

    // Allergies
    const allergyFacts = facts.filter(f => f.category === 'allergy');
    sections.push({
      id: uuid(), type: 'allergies', title: 'Allergies',
      content: allergyFacts.length > 0
        ? allergyFacts.map(f => `• ${f.valueNormalized || f.valueRaw} (${f.source.replace('_', '-')})`).join('\n')
        : 'No known allergies reported.',
      facts: allergyFacts.map(f => f.id), reviewStatus: 'pending',
    });

    // Family History
    const fhxFacts = facts.filter(f => f.category === 'family_history');
    sections.push({
      id: uuid(), type: 'family_history', title: 'Family History',
      content: fhxFacts.length > 0
        ? fhxFacts.map(f => `• ${f.valueNormalized || f.valueRaw}`).join('\n')
        : 'Not assessed in this session.',
      facts: fhxFacts.map(f => f.id), reviewStatus: 'pending',
    });

    // Personal History
    const phFacts = facts.filter(f => f.category === 'personal_history' || f.category === 'lifestyle');
    sections.push({
      id: uuid(), type: 'personal_history', title: 'Personal History',
      content: phFacts.length > 0
        ? phFacts.map(f => `• ${f.conceptLabel}: ${f.valueNormalized || f.valueRaw}`).join('\n')
        : 'Not assessed in this session.',
      facts: phFacts.map(f => f.id), reviewStatus: 'pending',
    });

    // ROS
    const rosFacts = facts.filter(f => f.category === 'ros');
    sections.push({
      id: uuid(), type: 'review_of_systems', title: 'Review of Systems',
      content: rosFacts.length > 0
        ? rosFacts.map(f => `• ${f.conceptLabel}: ${f.valueNormalized || f.valueRaw}`).join('\n')
        : 'Not assessed in this session.',
      facts: rosFacts.map(f => f.id), reviewStatus: 'pending',
    });

    return sections;
  }

  private buildHPIProse(hpiFacts: ClinicalFact[], allFacts: ClinicalFact[]): string {
    // Build a narrative from structured facts — this simulates what a constrained LLM would produce
    const parts: string[] = [];

    const cc = hpiFacts.find(f => f.category === 'chief_complaint');
    if (cc) parts.push(`Patient presents with ${cc.valueNormalized || cc.valueRaw}.`);

    const onset = hpiFacts.find(f => f.conceptCode === 'symptom_onset');
    if (onset) parts.push(`Onset: ${onset.valueNormalized || onset.valueRaw}.`);

    const location = hpiFacts.find(f => f.conceptCode === 'pain_location');
    if (location) parts.push(`Location: ${location.valueNormalized || location.valueRaw}.`);

    const character = hpiFacts.find(f => f.conceptCode === 'pain_character');
    if (character) parts.push(`Character: ${character.valueNormalized || character.valueRaw}.`);

    const severity = hpiFacts.find(f => f.conceptCode === 'pain_severity');
    if (severity) parts.push(`Severity: ${severity.valueNormalized || severity.valueRaw}.`);

    const radiation = hpiFacts.find(f => f.conceptCode === 'pain_radiation');
    if (radiation) parts.push(`Radiation: ${radiation.valueNormalized || radiation.valueRaw}.`);

    const associated = hpiFacts.find(f => f.conceptCode === 'associated_symptoms');
    if (associated) parts.push(`Associated symptoms: ${associated.valueNormalized || associated.valueRaw}.`);

    // Add relevant context from other fact categories
    const pmhFacts = allFacts.filter(f => f.category === 'past_medical_history' && !f.isConflicting);
    if (pmhFacts.length > 0) {
      const pmhList = pmhFacts.map(f => f.valueNormalized || f.valueRaw).join(', ');
      parts.push(`Relevant medical history includes: ${pmhList}.`);
    }

    return parts.join(' ') || 'History details were not fully captured in this session.';
  }

  private identifyMissing(
    answeredQuestionIds: string[],
    allRequiredQuestionIds: string[]
  ): MissingInfo[] {
    const missing: MissingInfo[] = [];

    // Map of common question IDs to human-readable labels
    const questionLabels: Record<string, { label: string; priority: 'required' | 'recommended' | 'optional' }> = {
      'q-cc': { label: 'Chief Complaint', priority: 'required' },
      'q-onset': { label: 'Onset', priority: 'required' },
      'q-location': { label: 'Location', priority: 'required' },
      'q-character': { label: 'Pain Character', priority: 'required' },
      'q-severity': { label: 'Severity', priority: 'required' },
      'q-radiation': { label: 'Radiation', priority: 'recommended' },
      'q-aggravating': { label: 'Aggravating Factors', priority: 'recommended' },
      'q-relieving': { label: 'Relieving Factors', priority: 'recommended' },
      'q-associated': { label: 'Associated Symptoms', priority: 'required' },
      'q-pmh-dm': { label: 'Diabetes History', priority: 'required' },
      'q-pmh-htn': { label: 'Hypertension History', priority: 'required' },
      'q-pmh-heart': { label: 'Heart Disease History', priority: 'required' },
      'q-pmh-asthma': { label: 'Asthma/COPD History', priority: 'required' },
      'q-pmh-surgery': { label: 'Past Surgeries', priority: 'recommended' },
      'q-med-current': { label: 'Current Medications', priority: 'required' },
      'q-allergy': { label: 'Allergies', priority: 'required' },
      'q-fhx-heart': { label: 'Family Heart Disease', priority: 'recommended' },
      'q-fhx-dm': { label: 'Family Diabetes', priority: 'recommended' },
      'q-smoking': { label: 'Smoking History', priority: 'recommended' },
      'q-alcohol': { label: 'Alcohol Use', priority: 'recommended' },
    };

    for (const qId of allRequiredQuestionIds) {
      if (!answeredQuestionIds.includes(qId)) {
        const info = questionLabels[qId];
        missing.push({
          fieldName: qId,
          fieldLabel: info?.label || qId,
          reason: 'not_asked',
          priority: info?.priority || 'optional',
        });
      }
    }

    return missing;
  }
}

export const summaryGenerator = new SummaryGenerator();
