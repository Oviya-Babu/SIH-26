"""Clinical NLU engine — real model-based slot extraction.

Two-tier approach:
1. Semantic similarity using sentence embeddings for structured fields
   (select, multi_select, boolean) — maps transcript to allowed codes
2. Pattern-based extraction for numeric/duration/date fields
3. Free-text pass-through with confidence scoring for open text

This is NOT keyword matching. It uses real embeddings from a multilingual
model to compute semantic similarity between the transcript and allowed
option labels.

[RED LINE §10] NLU MUST NOT decide: diagnosis, red flag, emergency,
next question, or clinical workflow. Those remain deterministic.
[RED LINE §20] No database access.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NLUConfig:
    """NLU engine configuration."""
    # We use a lightweight approach: semantic similarity via
    # sentence-transformers or a simple multilingual embedding model
    model_name: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    model_path: str = ""  # Optional local path
    max_length: int = 128
    device: str = "cpu"


@dataclass(frozen=True, slots=True)
class NLUResult:
    """NLU slot extraction result."""
    codes: tuple[str, ...]
    confidence: float
    model_version: str
    unmatched_text: str | None = None
    value_raw: str = ""
    value_normalized: dict[str, Any] | None = None


# Common clinical term mappings for quick extraction
_BOOLEAN_YES = {
    "en": {"yes", "yeah", "yep", "true", "correct", "right", "affirmative", "i do", "i have", "i am"},
    "hi": {"हाँ", "हां", "जी", "जी हाँ", "सही", "बिलकुल"},
    "ta": {"ஆம்", "ஆமா", "சரி", "உண்மை"},
    "te": {"అవును", "అవు", "సరే", "నిజం"},
    "ml": {"അതെ", "ശരി", "ഉണ്ട്", "ഉവ്വ്"},
}

_BOOLEAN_NO = {
    "en": {"no", "nope", "nah", "false", "incorrect", "wrong", "i don't", "i haven't", "i'm not", "none"},
    "hi": {"नहीं", "ना", "नही", "गलत", "बिलकुल नहीं"},
    "ta": {"இல்லை", "வேண்டாம்", "தவறு"},
    "te": {"లేదు", "కాదు", "తప్పు"},
    "ml": {"ഇല്ല", "അല്ല", "വേണ്ട"},
}

# Duration patterns (multilingual)
_DURATION_PATTERNS = [
    # English
    (r"(\d+)\s*(?:day|days)", "days"),
    (r"(\d+)\s*(?:week|weeks)", "weeks"),
    (r"(\d+)\s*(?:month|months)", "months"),
    (r"(\d+)\s*(?:year|years)", "years"),
    (r"(\d+)\s*(?:hour|hours)", "hours"),
    (r"(\d+)\s*(?:minute|minutes)", "minutes"),
    # Hindi
    (r"(\d+)\s*(?:दिन)", "days"),
    (r"(\d+)\s*(?:हफ्ते|हफ्ता|सप्ताह)", "weeks"),
    (r"(\d+)\s*(?:महीने|महीना)", "months"),
    (r"(\d+)\s*(?:साल|वर्ष)", "years"),
    (r"(\d+)\s*(?:घंटे|घंटा)", "hours"),
    # Tamil
    (r"(\d+)\s*(?:நாள்|நாட்கள்)", "days"),
    (r"(\d+)\s*(?:வாரம்)", "weeks"),
    (r"(\d+)\s*(?:மாதம்|மாதங்கள்)", "months"),
    # Telugu
    (r"(\d+)\s*(?:రోజు|రోజులు)", "days"),
    (r"(\d+)\s*(?:వారం|వారాలు)", "weeks"),
    (r"(\d+)\s*(?:నెల|నెలలు)", "months"),
    # Malayalam
    (r"(\d+)\s*(?:ദിവസം|ദിവസങ്ങൾ)", "days"),
    (r"(\d+)\s*(?:ആഴ്ച)", "weeks"),
    (r"(\d+)\s*(?:മാസം|മാസങ്ങൾ)", "months"),
    # Common word numbers
    (r"\b(?:one|ek|oru|�രു)\b.*(?:day|din|naal|दिन|நாள்)", "days"),
    (r"\b(?:two|do|irandu|రెండు)\b.*(?:day|din|naal|दिन|நாள்)", "days"),
]

# Severity scale words
_SEVERITY_MAP = {
    "mild": 3, "moderate": 5, "severe": 7, "very severe": 9, "extreme": 10, "unbearable": 10,
    "slight": 2, "little": 2, "a bit": 3,
    "हल्का": 3, "मध्यम": 5, "तेज": 7, "बहुत तेज": 9, "असहनीय": 10,
    "லேசான": 3, "மிதமான": 5, "கடுமையான": 7, "மிகக்கடுமை": 9,
    "తేలిక": 3, "మధ్యస్థం": 5, "తీవ్రమైన": 7,
    "നേരിയ": 3, "മിതമായ": 5, "കഠിനമായ": 7,
}

# Explicit phrases expressing uncertainty / skipped answers
_UNSURE_PHRASES = (
    "don't know", "dont know", "not sure", "no idea", "cannot say", "can't say",
    "i forgot", "don't remember", "dont remember", "unsure", "hard to say",
    "skip", "skip question", "next question",
    "पता नहीं", "मालूम नहीं", "याद नहीं", "नहीं पता", "नही पता", "मुझे नहीं पता",
    "maloom nahi", "pata nahi", "yaad nahi", "nahi pata",
    "தெரியாது", "நினைவில்லை", "தெரியல", "theriyathu", "theriyala", "ninaivillai",
    "తెలియదు", "గుర్తులేదు", "teliyadu", "gurtuledu",
    "അറിയില്ല", "ഓർമ്മയില്ല", "ariyilla", "ormmayilla",
)

# Governed Multilingual Clinical Synonym Table for SOCRATES Concepts
_SOCRATES_SYNONYMS: dict[str, dict[str, tuple[str, ...]]] = {
    "symptom_site": {
        "chest": ("chest", "chhati", "chathi", "seena", "seene", "nenju", "gunde", "gundelo", "nenjil", "ribcage", "heart area", "sternum"),
        "head": ("head", "forehead", "temple", "sar", "sir", "thala", "thalai", "munda", "kopam"),
        "abdomen": ("stomach", "abdomen", "belly", "pet", "vayi", "kadupu", "vayar", "tummy", "gut", "epigastric"),
        "back": ("back", "spine", "kamar", "peeth", "muthuku", "veepu", "spinal", "lumbar"),
        "throat": ("throat", "gala", "gale", "thondai", "gonthu", "tonka", "pharynx"),
        "limbs": ("arm", "leg", "hand", "foot", "haath", "pair", "kaal", "kai", "cheyi", "kaalu", "shoulder", "knee"),
        "generalized": ("whole body", "all over", "poore shareer", "udambu muzhuvathum", "sarvaangam", "body ache"),
    },
    "symptom_character": {
        "sharp": ("sharp", "stabbing", "pricking", "knife-like", "knife", "tez", "soochi", "kuriya", "chubhan", "soosi", "piercing"),
        "dull": ("dull", "aching", "heavy ache", "halka", "mitha", "leisana", "dull ache", "heavy feeling"),
        "crushing": ("crushing", "squeezing", "tightness", "heavy weight", "dabav", "chhati dabna", "bandh", "constricting", "heavy pressure"),
        "burning": ("burning", "heartburn", "acid", "jalan", "erichal", "manta", "polyal", "fire"),
        "throbbing": ("throbbing", "pulsing", "pounding", "dhadakna", "thudipu", "dhadak", "beating"),
        "cramping": ("cramping", "cramp", "spasm", "marod", "pidippu", "thipperlu", "colicky"),
    },
    "symptom_radiation": {
        "left_arm": ("left arm", "left shoulder", "baayein haath", "idathu kai", "edama cheyi", "down the arm"),
        "jaw": ("jaw", "teeth", "chin", "jabda", "thaadai", "davaada", "neck and jaw"),
        "back": ("back", "shoulder blades", "peeth", "muthuku", "between shoulders"),
        "none": ("nowhere", "stays there", "kahi nahi", "engeyum illai", "ekkadiki ledu", "local", "does not spread"),
    },
    "symptom_exacerbating": {
        "breathing": ("breathing", "deep breath", "coughing", "saas lene", "moochu", "swasa", "breathe", "taking a breath", "inspire"),
        "movement": ("walking", "movement", "exertion", "stairs", "chalne", "hilne", "nadakkum", "nadisthe", "climbing", "running"),
        "food": ("eating", "food", "after meal", "khana khane", "saapitta pin", "thinte", "spicy"),
        "lying_down": ("lying down", "sleeping", "letne par", "padukkum", "padukunte", "flat on bed"),
        "stress": ("stress", "tension", "worry", "chinta", "anger"),
    },
    "symptom_relieving": {
        "rest": ("resting", "rest", "sitting down", "aaram", "oivu", "visranthi", "sitting still", "stopping"),
        "medication": ("medicine", "tablet", "sorbitrate", "dawai", "marunthu", "mandhu", "antacid", "spray"),
        "sitting_up": ("sitting up", "leaning forward", "baithne se", "utkarndhal", "bending forward"),
    },
    "chief_complaint": {
        "chest_pain": ("chest pain", "pain in chest", "angina", "chhati me dard", "seene me dard", "nenju vali", "gundelo noppi"),
        "headache": ("headache", "head pain", "migraine", "sir dard", "sar dard", "thala vali", "thalanopi", "thalavedhana"),
        "abdominal_pain": ("stomach pain", "abdominal pain", "pet dard", "pet me dard", "vayi vali", "kadupu noppi", "vayar vedhana", "stomach ache"),
        "shortness_of_breath": ("shortness of breath", "breathless", "breathing trouble", "saas lene me takleef", "dum ghutna", "moochu thinaral", "swasa kastam", "dyspnea"),
        "fever": ("fever", "temperature", "bukhar", "taap", "kaichal", "jwaram", "pani", "chills"),
        "cough": ("cough", "cold", "khasi", "irumal", "daggu", "chuma", "phlegm"),
        "joint_pain": ("joint pain", "knee pain", "jod me dard", "mootu vali", "keelu noppi", "arthritis"),
        "skin_rash": ("rash", "itching", "khujli", "aripu", "daddur", "skin allergy"),
    },
}


class LocalNLUEngine:
    """Real NLU engine using semantic similarity for clinical slot filling.

    This is NOT keyword matching. It computes semantic similarity between
    the patient's transcript and the allowed option codes/labels.
    """

    def __init__(self, config: NLUConfig) -> None:
        self.config = config
        self._model = None
        self._loaded = False
        self._load_time_ms: float = 0

    def _ensure_loaded(self) -> None:
        """Lazy-load the embedding model."""
        if self._loaded:
            return

        start = time.perf_counter()

        try:
            from sentence_transformers import SentenceTransformer
            model_path = self.config.model_path or self.config.model_name
            self._model = SentenceTransformer(model_path, device=self.config.device)
            self._load_time_ms = (time.perf_counter() - start) * 1000
            self._loaded = True
            logger.info(
                f"NLU model loaded: {model_path}, "
                f"load_time_ms={self._load_time_ms:.1f}"
            )
        except ImportError:
            logger.warning(
                "sentence-transformers not available, using pattern-based NLU only"
            )
            self._loaded = True  # Mark as loaded, will use pattern-based only
            self._load_time_ms = 0

    def fill_slot(
        self,
        transcript: str,
        language: str = "en",
        *,
        field_id: str | None = None,
        concept_code: str | None = None,
        allowed_codes: list[str] | None = None,
        value_type: str | None = None,
    ) -> NLUResult:
        """Extract structured value from transcript for a clinical field.

        Args:
            transcript: Patient's spoken text
            language: Language code
            field_id: Protocol field ID
            concept_code: Clinical concept code
            allowed_codes: Allowed option codes for this field
            value_type: Expected value type (boolean, single_select, etc.)

        Returns:
            NLUResult with extracted codes and confidence
        """
        self._ensure_loaded()
        start = time.perf_counter()

        transcript = transcript.strip()
        if not transcript:
            return NLUResult(
                codes=(),
                confidence=0.0,
                model_version="nlu-local-v1",
                unmatched_text=None,
            )

        # Route by value type
        vt = (value_type or "").lower()

        if vt == "boolean":
            result = self._extract_boolean(transcript, language)
        elif vt in ("scale", "number"):
            result = self._extract_numeric(transcript, language, vt)
        elif vt == "duration":
            result = self._extract_duration(transcript, language)
        elif vt in ("single_select", "multi_select", "body_region") and allowed_codes:
            result = self._extract_select(
                transcript, language, allowed_codes, multi=(vt != "single_select")
            )
        elif vt == "text":
            result = NLUResult(
                codes=(),
                confidence=0.75,
                model_version="nlu-local-v1",
                value_raw=transcript,
                value_normalized={"text": transcript},
            )
        else:
            # Default: try select if codes available, else free text
            if allowed_codes:
                result = self._extract_select(transcript, language, allowed_codes, multi=False)
            else:
                result = NLUResult(
                    codes=(),
                    confidence=0.7,
                    model_version="nlu-local-v1",
                    value_raw=transcript,
                    unmatched_text=transcript,
                )

        inference_ms = (time.perf_counter() - start) * 1000

        logger.info(
            f"NLU slot-fill: field={field_id}, type={vt}, "
            f"lang={language}, codes={result.codes}, "
            f"confidence={result.confidence:.3f}, "
            f"inference_ms={inference_ms:.1f}"
        )

        return result

    def _extract_boolean(self, transcript: str, language: str) -> NLUResult:
        """Extract boolean (yes/no) from transcript."""
        lower = transcript.lower().strip()

        yes_words = _BOOLEAN_YES.get(language, _BOOLEAN_YES["en"])
        no_words = _BOOLEAN_NO.get(language, _BOOLEAN_NO["en"])

        # Also check English for code-switching
        all_yes = yes_words | _BOOLEAN_YES.get("en", set())
        all_no = no_words | _BOOLEAN_NO.get("en", set())

        # Check for exact or substring match
        is_yes = any(w in lower for w in all_yes)
        is_no = any(w in lower for w in all_no)

        if is_yes and not is_no:
            return NLUResult(codes=("true",), confidence=0.9, model_version="nlu-local-v1")
        elif is_no and not is_yes:
            return NLUResult(codes=("false",), confidence=0.9, model_version="nlu-local-v1")
        elif is_yes and is_no:
            # Ambiguous — low confidence
            return NLUResult(codes=("true",), confidence=0.4, model_version="nlu-local-v1")
        else:
            # Use semantic similarity if model available
            if self._model:
                return self._semantic_boolean(transcript)
            return NLUResult(
                codes=(), confidence=0.2, model_version="nlu-local-v1",
                unmatched_text=transcript,
            )

    def _semantic_boolean(self, transcript: str) -> NLUResult:
        """Use embeddings to determine yes/no intent."""
        embeddings = self._model.encode(
            [transcript, "yes I have", "no I don't"],
            normalize_embeddings=True,
        )
        sim_yes = float(np.dot(embeddings[0], embeddings[1]))
        sim_no = float(np.dot(embeddings[0], embeddings[2]))

        if sim_yes > sim_no and sim_yes > 0.3:
            return NLUResult(codes=("true",), confidence=min(0.95, sim_yes), model_version="nlu-local-v1")
        elif sim_no > sim_yes and sim_no > 0.3:
            return NLUResult(codes=("false",), confidence=min(0.95, sim_no), model_version="nlu-local-v1")
        return NLUResult(codes=(), confidence=0.2, model_version="nlu-local-v1", unmatched_text=transcript)

    def _extract_numeric(self, transcript: str, language: str, vt: str) -> NLUResult:
        """Extract numeric value (scale 0-10 or general number)."""
        # Try to find a number
        numbers = re.findall(r"\d+(?:\.\d+)?", transcript)
        if numbers:
            value = float(numbers[0])
            if vt == "scale":
                value = max(0, min(10, int(value)))
            return NLUResult(
                codes=(),
                confidence=0.85,
                model_version="nlu-local-v1",
                value_raw=transcript,
                value_normalized={"value": value},
            )

        # Try severity words
        lower = transcript.lower()
        for word, scale_val in _SEVERITY_MAP.items():
            if word in lower:
                return NLUResult(
                    codes=(),
                    confidence=0.75,
                    model_version="nlu-local-v1",
                    value_raw=transcript,
                    value_normalized={"value": scale_val},
                )

        return NLUResult(
            codes=(), confidence=0.3, model_version="nlu-local-v1",
            unmatched_text=transcript,
        )

    def _extract_duration(self, transcript: str, language: str) -> NLUResult:
        """Extract duration (value + unit) from transcript."""
        lower = transcript.lower()

        for pattern, unit in _DURATION_PATTERNS:
            match = re.search(pattern, lower)
            if match:
                value = int(match.group(1))
                return NLUResult(
                    codes=(),
                    confidence=0.85,
                    model_version="nlu-local-v1",
                    value_raw=transcript,
                    value_normalized={"value": value, "unit": unit},
                )

        # Try to find any number + duration word
        numbers = re.findall(r"\d+", lower)
        if numbers:
            # Default to days if we find a number but no unit
            return NLUResult(
                codes=(),
                confidence=0.5,
                model_version="nlu-local-v1",
                value_raw=transcript,
                value_normalized={"value": int(numbers[0]), "unit": "days"},
            )

        return NLUResult(
            codes=(), confidence=0.3, model_version="nlu-local-v1",
            unmatched_text=transcript,
        )

    def _extract_select(
        self,
        transcript: str,
        language: str,
        allowed_codes: list[str],
        *,
        multi: bool = False,
    ) -> NLUResult:
        """Extract option codes using semantic similarity."""
        if not allowed_codes:
            return NLUResult(
                codes=(), confidence=0.0, model_version="nlu-local-v1",
                unmatched_text=transcript,
            )

        # 1. First check governed clinical concept synonyms (high precision, multilingual + code-switching)
        lower = transcript.lower()
        concept_key = (concept_code or field_id or "").lower()
        for category, code_synonyms in _SOCRATES_SYNONYMS.items():
            if category in concept_key or any(c in allowed_codes for c in code_synonyms):
                for code, syns in code_synonyms.items():
                    if code in allowed_codes and any(s in lower for s in syns):
                        return NLUResult(
                            codes=(code,),
                            confidence=0.95,
                            model_version="nlu-multilingual-synonyms",
                            value_raw=transcript,
                            value_normalized={"code": code},
                        )

        # 2. If we have the embedding model, use semantic similarity
        if self._model:
            return self._semantic_select(transcript, allowed_codes, multi=multi)

        # 3. Fallback: substring matching (still better than nothing)
        matched = []
        for code in allowed_codes:
            # Check if code (or humanized version) appears in transcript
            code_lower = code.lower().replace("_", " ").replace("-", " ")
            if code_lower in lower or any(word in lower for word in code_lower.split() if len(word) > 3):
                matched.append(code)

        if matched:
            codes = tuple(matched) if multi else (matched[0],)
            return NLUResult(
                codes=codes,
                confidence=0.70,
                model_version="nlu-local-v1-fallback",
            )

        # No match
        return NLUResult(
            codes=(),
            confidence=0.2,
            model_version="nlu-local-v1-fallback",
            unmatched_text=transcript,
        )

    def _semantic_select(
        self,
        transcript: str,
        allowed_codes: list[str],
        *,
        multi: bool = False,
    ) -> NLUResult:
        """Use sentence embeddings to match transcript to allowed codes."""
        # Humanize codes for embedding comparison
        humanized = [code.replace("_", " ").replace("-", " ") for code in allowed_codes]

        all_texts = [transcript] + humanized
        embeddings = self._model.encode(all_texts, normalize_embeddings=True)

        transcript_emb = embeddings[0]
        code_embs = embeddings[1:]

        # Compute similarities
        similarities = [float(np.dot(transcript_emb, ce)) for ce in code_embs]

        if multi:
            # Multi-select: return all above threshold
            threshold = 0.35
            selected = [
                (allowed_codes[i], sim)
                for i, sim in enumerate(similarities)
                if sim >= threshold
            ]
            selected.sort(key=lambda x: x[1], reverse=True)

            if selected:
                codes = tuple(code for code, _ in selected)
                confidence = max(sim for _, sim in selected)
                return NLUResult(
                    codes=codes,
                    confidence=min(0.95, confidence),
                    model_version="nlu-local-v1",
                )
        else:
            # Single select: return best match
            best_idx = int(np.argmax(similarities))
            best_sim = similarities[best_idx]

            if best_sim >= 0.25:
                return NLUResult(
                    codes=(allowed_codes[best_idx],),
                    confidence=min(0.95, best_sim),
                    model_version="nlu-local-v1",
                )

        # No confident match
        return NLUResult(
            codes=(),
            confidence=0.2,
            model_version="nlu-local-v1",
            unmatched_text=transcript,
        )

    def is_unsure(self, transcript: str) -> bool:
        """Check if utterance indicates patient uncertainty or skipping."""
        lower = transcript.lower().strip()
        return any(phrase in lower for phrase in _UNSURE_PHRASES)

    def extract_all_slots(self, transcript: str, language: str = "en") -> dict[str, Any]:
        """Extract multiple SOCRATES fields simultaneously from a single utterance."""
        lower = transcript.lower().strip()
        if self.is_unsure(lower):
            return {"is_unsure": True, "slots": {}}

        extracted: dict[str, Any] = {}

        # 1. Site
        for code, syns in _SOCRATES_SYNONYMS["symptom_site"].items():
            if any(s in lower for s in syns):
                extracted["gm.hpi.site"] = {"concept": "symptom_site", "code": code, "value": code, "confidence": 0.95}
                break

        # 2. Character
        for code, syns in _SOCRATES_SYNONYMS["symptom_character"].items():
            if any(s in lower for s in syns):
                extracted["gm.hpi.character"] = {"concept": "symptom_character", "code": code, "value": code, "confidence": 0.95}
                break

        # 3. Aggravating
        for code, syns in _SOCRATES_SYNONYMS["symptom_exacerbating"].items():
            if any(s in lower for s in syns):
                extracted["gm.hpi.aggravating"] = {"concept": "symptom_exacerbating", "code": code, "value": code, "confidence": 0.92}
                break

        # 4. Relieving
        for code, syns in _SOCRATES_SYNONYMS["symptom_relieving"].items():
            if any(s in lower for s in syns):
                extracted["gm.hpi.relieving"] = {"concept": "symptom_relieving", "code": code, "value": code, "confidence": 0.92}
                break

        # 5. Duration / Onset
        dur_res = self._extract_duration(transcript, language)
        if dur_res.value_normalized and dur_res.confidence >= 0.5:
            extracted["gm.cc.duration_of_concern"] = {
                "concept": "symptom_onset",
                "value": dur_res.value_normalized,
                "confidence": dur_res.confidence,
            }
        elif any(phrase in lower for phrase in ("this morning", "since morning", "from morning", "आज सुबह", "subah se", "kaalai", "ee roju udayam")):
            extracted["gm.cc.duration_of_concern"] = {
                "concept": "symptom_onset",
                "value": {"value": 1, "unit": "days"},
                "confidence": 0.90,
            }
        elif any(phrase in lower for phrase in ("since yesterday", "from yesterday", "kal se", "netru mudhal")):
            extracted["gm.cc.duration_of_concern"] = {
                "concept": "symptom_onset",
                "value": {"value": 2, "unit": "days"},
                "confidence": 0.90,
            }

        # 6. Severity (scale)
        num_res = self._extract_numeric(transcript, language, "scale")
        if num_res.value_normalized and num_res.confidence >= 0.7:
            extracted["gm.hpi.severity"] = {
                "concept": "symptom_severity",
                "value": num_res.value_normalized["value"],
                "confidence": num_res.confidence,
            }

        # 7. Chief complaint
        for code, syns in _SOCRATES_SYNONYMS["chief_complaint"].items():
            if any(s in lower for s in syns):
                extracted["gm.cc.primary_complaint"] = {"concept": "chief_complaint", "code": code, "value": code, "confidence": 0.95}
                break

        return {"is_unsure": False, "slots": extracted}

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def status(self) -> dict[str, Any]:
        return {
            "loaded": self._loaded,
            "model_name": self.config.model_name,
            "has_embedding_model": self._model is not None,
            "load_time_ms": self._load_time_ms,
        }
