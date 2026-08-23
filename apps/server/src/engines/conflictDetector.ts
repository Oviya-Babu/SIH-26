// ─── Conflict Detector ───────────────────────────────────────────────────────
// PRD Section 11: Compares structured facts across sources.
// On contradiction, the system does NOT pick a winner — it surfaces both sides.

import type { ClinicalFact, ConflictInfo } from '@medikiosk/shared';
import { v4 as uuid } from 'uuid';

class ConflictDetector {
  /**
   * Detect conflicts across clinical facts from different sources.
   * A conflict occurs when two facts about the same concept disagree.
   */
  detectConflicts(facts: ClinicalFact[]): ConflictInfo[] {
    const conflicts: ConflictInfo[] = [];
    const factsByCode = new Map<string, ClinicalFact[]>();

    // Group facts by concept code
    for (const fact of facts) {
      const existing = factsByCode.get(fact.conceptCode) || [];
      existing.push(fact);
      factsByCode.set(fact.conceptCode, existing);
    }

    // For each concept with multiple facts from different sources, check for conflicts
    for (const [code, codeFacts] of factsByCode) {
      if (codeFacts.length < 2) continue;

      const patientReported = codeFacts.filter(f => f.source === 'patient_reported');
      const documentExtracted = codeFacts.filter(f => f.source === 'document_extracted');

      // Check patient-reported vs document-extracted conflicts
      for (const pr of patientReported) {
        for (const de of documentExtracted) {
          if (this.areConflicting(pr, de)) {
            conflicts.push({
              id: uuid(),
              field: code,
              fieldLabel: pr.conceptLabel || de.conceptLabel,
              sources: [
                {
                  value: pr.valueNormalized || pr.valueRaw,
                  source: `Patient (today, self-report)`,
                  sourceType: 'patient_reported',
                  provenanceRef: pr.provenanceRef,
                  timestamp: pr.timestamp,
                },
                {
                  value: de.valueNormalized || de.valueRaw,
                  source: (de.metadata?.documentSource as string) || 'Uploaded document',
                  sourceType: 'document_extracted',
                  provenanceRef: de.provenanceRef,
                  timestamp: de.timestamp,
                },
              ],
            });
          }
        }
      }

      // Also check document vs document conflicts
      for (let i = 0; i < documentExtracted.length; i++) {
        for (let j = i + 1; j < documentExtracted.length; j++) {
          if (this.areConflicting(documentExtracted[i], documentExtracted[j])) {
            conflicts.push({
              id: uuid(),
              field: code,
              fieldLabel: documentExtracted[i].conceptLabel,
              sources: [
                {
                  value: documentExtracted[i].valueNormalized || documentExtracted[i].valueRaw,
                  source: (documentExtracted[i].metadata?.documentSource as string) || 'Document 1',
                  sourceType: 'document_extracted',
                  provenanceRef: documentExtracted[i].provenanceRef,
                  timestamp: documentExtracted[i].timestamp,
                },
                {
                  value: documentExtracted[j].valueNormalized || documentExtracted[j].valueRaw,
                  source: (documentExtracted[j].metadata?.documentSource as string) || 'Document 2',
                  sourceType: 'document_extracted',
                  provenanceRef: documentExtracted[j].provenanceRef,
                  timestamp: documentExtracted[j].timestamp,
                },
              ],
            });
          }
        }
      }
    }

    return conflicts;
  }

  private areConflicting(factA: ClinicalFact, factB: ClinicalFact): boolean {
    // Normalize values for comparison
    const valA = this.normalizeForComparison(factA.valueNormalized || factA.valueRaw);
    const valB = this.normalizeForComparison(factB.valueNormalized || factB.valueRaw);

    // Check for direct contradiction patterns
    // Pattern 1: One denies, the other confirms
    const denyPatterns = ['no', 'denies', 'denied', 'negative', 'none', 'never', 'absent'];
    const confirmPatterns = ['yes', 'positive', 'present', 'diagnosed', 'confirmed', 'documented'];

    const aDenies = denyPatterns.some(p => valA.includes(p));
    const aConfirms = confirmPatterns.some(p => valA.includes(p));
    const bDenies = denyPatterns.some(p => valB.includes(p));
    const bConfirms = confirmPatterns.some(p => valB.includes(p));

    if ((aDenies && bConfirms) || (aConfirms && bDenies)) return true;

    // Pattern 2: Significantly different numeric values
    const numA = this.extractNumber(valA);
    const numB = this.extractNumber(valB);
    if (numA !== null && numB !== null) {
      const diff = Math.abs(numA - numB) / Math.max(numA, numB, 1);
      if (diff > 0.5) return true; // >50% difference
    }

    // Pattern 3: Both are non-empty but clearly different text values for the same concept
    if (valA && valB && valA !== valB && !valA.includes(valB) && !valB.includes(valA)) {
      // Only flag if both are substantive (not just formatting differences)
      if (valA.length > 3 && valB.length > 3) {
        return true;
      }
    }

    return false;
  }

  private normalizeForComparison(value: string): string {
    return value.toLowerCase().trim().replace(/[.,;:]/g, '');
  }

  private extractNumber(value: string): number | null {
    const match = value.match(/\d+(\.\d+)?/);
    return match ? parseFloat(match[0]) : null;
  }
}

export const conflictDetector = new ConflictDetector();
