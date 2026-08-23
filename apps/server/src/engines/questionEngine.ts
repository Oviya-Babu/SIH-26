// ─── Question Engine ─────────────────────────────────────────────────────────
// Deterministic state machine that drives the clinical interview
// PRD Sections 7-8: The LLM does NOT decide what to ask next — this engine does.

import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const protocolData = require('../data/protocols/general_medicine_v1.json');
import type { Answer, QuestionWidgetType } from '@medikiosk/shared';

export interface ProtocolQuestion {
  id: string;
  conceptCode: string;
  conceptLabel: string;
  category: string;
  fieldName: string;
  dataType: string;
  widgetType: QuestionWidgetType;
  required: boolean;
  options?: { value: string; label: Record<string, string> }[];
  voicePrompt: Record<string, string>;
  touchLabel: Record<string, string>;
  helpText?: Record<string, string>;
  dependsOn?: { field: string; operator: string; value: unknown }[];
  confirmBack?: boolean;
  group: string;
  order: number;
}

export interface QuestionGroup {
  id: string;
  label: Record<string, string>;
  order: number;
  questions: ProtocolQuestion[];
}

export interface NextQuestion {
  question: {
    id: string;
    conceptCode: string;
    conceptLabel: string;
    voicePrompt: string;
    touchLabel: string;
    widgetType: string;
    options?: { value: string; label: string; icon?: string }[];
    required: boolean;
    confirmBack: boolean;
    helpText?: string;
  };
  progress: {
    answeredCount: number;
    totalRequired: number;
    completenessScore: number;
    currentGroup: string;
    currentGroupLabel: string;
    groupProgress: number;
    totalGroups: number;
    currentGroupIndex: number;
  };
}

class QuestionEngine {
  private protocol: { questionGroups: QuestionGroup[] };

  constructor() {
    this.protocol = protocolData as { questionGroups: QuestionGroup[] };
  }

  /** Get all questions flattened, in order */
  getAllQuestions(): ProtocolQuestion[] {
    return this.protocol.questionGroups
      .sort((a, b) => a.order - b.order)
      .flatMap(g => g.questions.sort((a, b) => a.order - b.order));
  }

  /** Get all required questions that are applicable given current answers */
  getApplicableQuestions(answeredMap: Map<string, unknown>): ProtocolQuestion[] {
    return this.getAllQuestions().filter(q => {
      if (!q.dependsOn || q.dependsOn.length === 0) return true;
      return q.dependsOn.every(dep => this.evaluateDependency(dep, answeredMap));
    });
  }

  /** Compute completeness score */
  getCompletenessScore(answeredQuestionIds: string[], answeredMap: Map<string, unknown>): number {
    const applicable = this.getApplicableQuestions(answeredMap);
    const requiredApplicable = applicable.filter(q => q.required);
    if (requiredApplicable.length === 0) return 1;

    const answeredRequired = requiredApplicable.filter(q =>
      answeredQuestionIds.includes(q.id)
    );
    return answeredRequired.length / requiredApplicable.length;
  }

  /** Determine the next unanswered question (deterministic state machine) */
  getNextQuestion(
    answeredQuestionIds: string[],
    answeredMap: Map<string, unknown>,
    language: string = 'en'
  ): NextQuestion | null {
    const applicable = this.getApplicableQuestions(answeredMap);
    const unanswered = applicable.filter(q => !answeredQuestionIds.includes(q.id));

    // Prioritize required questions first, then optional
    const nextRequired = unanswered.find(q => q.required);
    const nextQuestion = nextRequired || unanswered[0];

    if (!nextQuestion) return null; // All questions answered

    // Find which group this question belongs to
    const currentGroup = this.protocol.questionGroups.find(g =>
      g.questions.some(q => q.id === nextQuestion.id)
    )!;
    const groupQuestions = currentGroup.questions.filter(q =>
      applicable.some(a => a.id === q.id)
    );
    const groupAnswered = groupQuestions.filter(q =>
      answeredQuestionIds.includes(q.id)
    );

    const totalRequired = applicable.filter(q => q.required).length;
    const answeredRequired = applicable.filter(q =>
      q.required && answeredQuestionIds.includes(q.id)
    ).length;

    const sortedGroups = this.protocol.questionGroups.sort((a, b) => a.order - b.order);
    const currentGroupIndex = sortedGroups.findIndex(g => g.id === currentGroup.id);

    return {
      question: {
        id: nextQuestion.id,
        conceptCode: nextQuestion.conceptCode,
        conceptLabel: nextQuestion.conceptLabel,
        voicePrompt: nextQuestion.voicePrompt[language] || nextQuestion.voicePrompt['en'],
        touchLabel: nextQuestion.touchLabel[language] || nextQuestion.touchLabel['en'],
        widgetType: nextQuestion.widgetType,
        options: nextQuestion.options?.map(o => ({
          value: o.value,
          label: o.label[language] || o.label['en'],
        })),
        required: nextQuestion.required,
        confirmBack: nextQuestion.confirmBack || false,
        helpText: nextQuestion.helpText?.[language] || nextQuestion.helpText?.['en'],
      },
      progress: {
        answeredCount: answeredQuestionIds.length,
        totalRequired,
        completenessScore: totalRequired > 0 ? answeredRequired / totalRequired : 1,
        currentGroup: currentGroup.id,
        currentGroupLabel: currentGroup.label[language] || currentGroup.label['en'],
        groupProgress: groupQuestions.length > 0
          ? groupAnswered.length / groupQuestions.length
          : 0,
        totalGroups: sortedGroups.length,
        currentGroupIndex,
      },
    };
  }

  /** Get all groups with their progress status */
  getGroupProgress(answeredQuestionIds: string[], answeredMap: Map<string, unknown>, language: string = 'en') {
    const applicable = this.getApplicableQuestions(answeredMap);

    return this.protocol.questionGroups
      .sort((a, b) => a.order - b.order)
      .map(group => {
        const groupApplicable = group.questions.filter(q =>
          applicable.some(a => a.id === q.id)
        );
        const groupAnswered = groupApplicable.filter(q =>
          answeredQuestionIds.includes(q.id)
        );

        return {
          id: group.id,
          label: group.label[language] || group.label['en'],
          totalQuestions: groupApplicable.length,
          answeredQuestions: groupAnswered.length,
          progress: groupApplicable.length > 0
            ? groupAnswered.length / groupApplicable.length
            : 0,
          isComplete: groupAnswered.length === groupApplicable.length,
        };
      });
  }

  /** Build an answer map from Answer objects */
  buildAnswerMap(answers: Answer[]): Map<string, unknown> {
    const allQuestions = this.getAllQuestions();
    const map = new Map<string, unknown>();

    for (const answer of answers) {
      const question = allQuestions.find(q => q.id === answer.questionId);
      if (question) {
        map.set(question.fieldName, answer.value);
      }
    }
    return map;
  }

  private evaluateDependency(
    dep: { field: string; operator: string; value: unknown },
    answeredMap: Map<string, unknown>
  ): boolean {
    const fieldValue = answeredMap.get(dep.field);

    switch (dep.operator) {
      case 'equals':
        return fieldValue === dep.value;
      case 'not_equals':
        return fieldValue !== dep.value;
      case 'exists':
        return dep.value ? fieldValue !== undefined && fieldValue !== null : !fieldValue;
      case 'in':
        return Array.isArray(dep.value) && dep.value.includes(fieldValue);
      case 'not_in':
        return Array.isArray(dep.value) && !dep.value.includes(fieldValue);
      case 'gt':
        return typeof fieldValue === 'number' && fieldValue > (dep.value as number);
      case 'lt':
        return typeof fieldValue === 'number' && fieldValue < (dep.value as number);
      default:
        return true;
    }
  }
}

export const questionEngine = new QuestionEngine();
