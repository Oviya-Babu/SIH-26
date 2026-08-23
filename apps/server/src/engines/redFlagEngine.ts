// ─── Red-Flag Safety Engine ──────────────────────────────────────────────────
// Fully deterministic, rule-based, versioned. Never delegated to LLM.
// PRD Section 9: Evaluated incrementally after EVERY answer, not just at session end.

import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const rulesData = require('../data/redflag-rules/emergency_v1.json');
import type { RedFlagAlert, RedFlagSeverity } from '@medikiosk/shared';
import { v4 as uuid } from 'uuid';

interface RuleCondition {
  field: string;
  operator: string;
  value: unknown;
}

interface Rule {
  id: string;
  name: string;
  description: string;
  version: string;
  severity: RedFlagSeverity;
  conditions: RuleCondition[];
  logicOperator: 'AND' | 'OR';
  escalationMessage: Record<string, string>;
  staffMessage: string;
  slaMinutes: number;
  category: string;
  active: boolean;
}

export interface RuleEvaluationResult {
  ruleId: string;
  ruleName: string;
  fired: boolean;
  severity?: RedFlagSeverity;
  matchedConditions?: Record<string, unknown>;
  staffMessage?: string;
  escalationMessage?: string;
  slaMinutes?: number;
}

class RedFlagEngine {
  private rules: Rule[];

  constructor() {
    this.rules = (rulesData.rules as Rule[]).filter(r => r.active);
  }

  /**
   * Evaluate all active rules against the current clinical state.
   * Returns all rule evaluations (fired AND not-fired) for audit logging,
   * and separately returns new alerts that should be created.
   */
  evaluate(
    sessionState: Map<string, unknown>,
    alreadyFiredRuleIds: Set<string>,
    sessionId: string,
    patientId: string,
    tenantId: string,
    language: string = 'en'
  ): {
    evaluations: RuleEvaluationResult[];
    newAlerts: RedFlagAlert[];
  } {
    const evaluations: RuleEvaluationResult[] = [];
    const newAlerts: RedFlagAlert[] = [];

    for (const rule of this.rules) {
      const conditionResults = rule.conditions.map(cond =>
        this.evaluateCondition(cond, sessionState)
      );

      const fired = rule.logicOperator === 'AND'
        ? conditionResults.every(r => r.matched)
        : conditionResults.some(r => r.matched);

      const evaluation: RuleEvaluationResult = {
        ruleId: rule.id,
        ruleName: rule.name,
        fired,
      };

      if (fired) {
        const matchedConditions: Record<string, unknown> = {};
        rule.conditions.forEach((cond, i) => {
          if (conditionResults[i].matched) {
            matchedConditions[cond.field] = sessionState.get(cond.field);
          }
        });

        evaluation.severity = rule.severity;
        evaluation.matchedConditions = matchedConditions;
        evaluation.staffMessage = rule.staffMessage;
        evaluation.escalationMessage = rule.escalationMessage[language] || rule.escalationMessage['en'];
        evaluation.slaMinutes = rule.slaMinutes;

        // Only create a new alert if this rule hasn't already fired for this session
        if (!alreadyFiredRuleIds.has(rule.id)) {
          const alert: RedFlagAlert = {
            id: uuid(),
            sessionId,
            patientId,
            tenantId,
            ruleId: rule.id,
            ruleName: rule.name,
            severity: rule.severity,
            status: 'active',
            matchedConditions,
            staffMessage: rule.staffMessage,
            firedAt: new Date().toISOString(),
            escalationLevel: 0,
          };
          newAlerts.push(alert);
        }
      }

      evaluations.push(evaluation);
    }

    return { evaluations, newAlerts };
  }

  /**
   * Get the calm, patient-facing message for a red flag.
   * Deliberately minimal — never discloses clinical details to avoid panic.
   */
  getPatientMessage(ruleId: string, language: string = 'en'): string {
    const rule = this.rules.find(r => r.id === ruleId);
    if (!rule) return language === 'hi'
      ? 'कृपया प्रतीक्षा करें, एक कर्मचारी जल्द ही आपकी सहायता करेगा।'
      : 'Please wait, a staff member will assist you shortly.';
    return rule.escalationMessage[language] || rule.escalationMessage['en'];
  }

  /** Get all active rules (for governance console) */
  getRules(): Rule[] {
    return this.rules;
  }

  private evaluateCondition(
    condition: RuleCondition,
    state: Map<string, unknown>
  ): { matched: boolean; fieldValue: unknown } {
    const fieldValue = state.get(condition.field);

    // If field doesn't exist in state, condition can't match (except 'exists' check)
    if (fieldValue === undefined || fieldValue === null) {
      if (condition.operator === 'exists' && condition.value === false) {
        return { matched: true, fieldValue };
      }
      return { matched: false, fieldValue };
    }

    let matched = false;

    switch (condition.operator) {
      case 'equals':
        matched = fieldValue === condition.value;
        break;

      case 'not_equals':
        matched = fieldValue !== condition.value;
        break;

      case 'contains': {
        const strValue = String(fieldValue).toLowerCase();
        const checkValue = String(condition.value).toLowerCase();
        if (Array.isArray(fieldValue)) {
          matched = fieldValue.some(v => String(v).toLowerCase().includes(checkValue));
        } else {
          matched = strValue.includes(checkValue);
        }
        break;
      }

      case 'in':
        if (Array.isArray(condition.value)) {
          matched = condition.value.includes(fieldValue);
        }
        break;

      case 'gt':
        matched = typeof fieldValue === 'number' && fieldValue > (condition.value as number);
        break;

      case 'lt':
        matched = typeof fieldValue === 'number' && fieldValue < (condition.value as number);
        break;

      case 'exists':
        matched = condition.value ? fieldValue !== undefined && fieldValue !== null : !fieldValue;
        break;

      default:
        matched = false;
    }

    return { matched, fieldValue };
  }
}

export const redFlagEngine = new RedFlagEngine();
