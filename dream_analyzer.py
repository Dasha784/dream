"""
Модуль анализа снов с использованием LLM
"""
from typing import Dict, List, Optional
import json
import openai
from config import OPENAI_API_KEY, ANALYSIS_MODEL

openai.api_key = OPENAI_API_KEY


class DreamAnalyzer:
    """Класс для анализа снов"""
    
    def __init__(self):
        self.model = ANALYSIS_MODEL
    
    def analyze_dream(self, dream_text: str, interpretation_type: str = "psychological") -> Dict:
        """
        Анализирует сон и возвращает полный отчёт
        
        Args:
            dream_text: Текст сна
            interpretation_type: Тип интерпретации (psychological, esoteric, emotional, archetypal)
        
        Returns:
            Словарь с результатами анализа
        """
        system_prompt = self._get_system_prompt(interpretation_type)
        user_prompt = self._get_user_prompt(dream_text, interpretation_type)
        
        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
            
            # Используем новый API OpenAI
            client = openai.OpenAI(api_key=OPENAI_API_KEY)
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.7,
                response_format={"type": "json_object"} if "gpt-4" in self.model.lower() else None
            )
            
            analysis_text = response.choices[0].message.content
            
            # Парсим JSON ответ
            try:
                analysis = json.loads(analysis_text)
            except json.JSONDecodeError:
                # Если не JSON, пытаемся извлечь структурированные данные
                analysis = self._parse_text_response(analysis_text)
            
            # Дополняем базовой структурой
            analysis = self._ensure_structure(analysis, dream_text, interpretation_type)
            
            return analysis
            
        except Exception as e:
            print(f"Ошибка анализа сна: {e}")
            # Возвращаем базовую структуру при ошибке
            return self._get_empty_analysis(interpretation_type)
    
    def _get_system_prompt(self, interpretation_type: str) -> str:
        """Получить системный промпт для анализа"""
        base_prompt = """Ты - эксперт по анализу снов. Твоя задача - провести глубокий анализ сна и предоставить структурированный отчёт.
        
Ты должен вернуть JSON с следующими полями:
- emotions: список объектов {"emotion": "название", "intensity": число от 0 до 1}
- locations: список мест из сна
- characters: список персонажей
- actions: список ключевых действий
- symbols: список символов {"symbol": "название", "meaning": "значение"}
- archetypes: список архетипов (например, Тень, Анима, Анимус, Мудрец, Герой и т.д.)
- emotional_tone: основной эмоциональный тон (одно слово: страх, радость, тревога, нежность, гнев и т.д.)
- themes: список тем сна (например, ревность, одиночество, рост, самоуважение)
- interpretation: подробная интерпретация (2-3 абзаца)
- advice: совет на день (1-2 предложения)
- lesson: урок, который несёт сон (1 предложение)"""
        
        if interpretation_type == "esoteric":
            base_prompt += "\n\nФокус на эзотерической интерпретации: мистические значения, предзнаменования, связь с кармой и духовным путём."
        elif interpretation_type == "emotional":
            base_prompt += "\n\nФокус на эмоциональном анализе: что чувствует человек, какие эмоциональные паттерны проявляются."
        elif interpretation_type == "archetypal":
            base_prompt += "\n\nФокус на архетипическом анализе в духе Карла Юнга: коллективное бессознательное, архетипы, символы."
        
        return base_prompt
    
    def _get_user_prompt(self, dream_text: str, interpretation_type: str) -> str:
        """Получить промпт пользователя"""
        return f"""Проанализируй следующий сон:

{dream_text}

Верни JSON с полным анализом согласно инструкциям. Будь внимателен к деталям и символам."""
    
    def _parse_text_response(self, text: str) -> Dict:
        """Парсит текстовый ответ, если JSON не получен"""
        # Базовая попытка извлечь информацию из текста
        return {
            "interpretation": text,
            "emotions": [],
            "locations": [],
            "characters": [],
            "actions": [],
            "symbols": [],
            "archetypes": [],
            "emotional_tone": "нейтральная",
            "themes": [],
            "advice": "",
            "lesson": ""
        }
    
    def _ensure_structure(self, analysis: Dict, dream_text: str, interpretation_type: str) -> Dict:
        """Обеспечивает наличие всех необходимых полей"""
        default_structure = {
            "emotions": [],
            "locations": [],
            "characters": [],
            "actions": [],
            "symbols": [],
            "archetypes": [],
            "emotional_tone": "нейтральная",
            "themes": [],
            "interpretation": "",
            "advice": "",
            "lesson": "",
            "interpretation_type": interpretation_type
        }
        
        # Объединяем с полученным анализом
        result = {**default_structure, **analysis}
        result["interpretation_type"] = interpretation_type
        
        return result
    
    def _get_empty_analysis(self, interpretation_type: str) -> Dict:
        """Возвращает пустую структуру анализа при ошибке"""
        return {
            "emotions": [],
            "locations": [],
            "characters": [],
            "actions": [],
            "symbols": [],
            "archetypes": [],
            "emotional_tone": "нейтральная",
            "themes": [],
            "interpretation": "К сожалению, не удалось провести анализ. Попробуйте ещё раз.",
            "advice": "",
            "lesson": "",
            "interpretation_type": interpretation_type
        }
    
    def answer_question(self, user_id: int, question: str, db, dream_analyzer) -> str:
        """
        Отвечает на вопрос пользователя о снах на основе его истории
        
        Args:
            user_id: ID пользователя
            question: Вопрос пользователя
            db: Экземпляр базы данных
            dream_analyzer: Экземпляр анализатора (для контекста)
        
        Returns:
            Ответ на вопрос
        """
        # Получаем последние сны пользователя
        recent_dreams = db.get_user_dreams(user_id, limit=10)
        patterns = db.get_user_patterns(user_id)
        statistics = db.get_dream_statistics(user_id)
        
        # Формируем контекст для LLM
        context = self._build_context(recent_dreams, patterns, statistics)
        
        prompt = f"""Пользователь задал вопрос о своих снах: "{question}"

Контекст снов пользователя:
{context}

Дай персональный ответ на основе анализа его снов, эмоциональных паттернов и повторяющихся тем. Будь конкретным и поддерживающим."""
        
        try:
            client = openai.OpenAI(api_key=OPENAI_API_KEY)
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "Ты помощник по анализу снов. Отвечай на основе данных о снах пользователя, будь конкретным и поддерживающим."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7
            )
            
            answer = response.choices[0].message.content
            return answer
            
        except Exception as e:
            print(f"Ошибка при ответе на вопрос: {e}")
            return "К сожалению, не удалось обработать ваш вопрос. Попробуйте переформулировать."
    
    def _build_context(self, dreams, patterns, statistics) -> str:
        """Строит контекст для ответа на вопрос"""
        context_parts = []
        
        if statistics:
            context_parts.append(f"Всего снов: {statistics.get('total_dreams', 0)}")
            
            if statistics.get('emotions_distribution'):
                context_parts.append(f"Распределение эмоций: {statistics['emotions_distribution']}")
            
            if statistics.get('themes_distribution'):
                top_themes = sorted(
                    statistics['themes_distribution'].items(),
                    key=lambda x: x[1],
                    reverse=True
                )[:5]
                context_parts.append(f"Повторяющиеся темы: {dict(top_themes)}")
        
        if patterns:
            if patterns.get('themes'):
                top_themes = sorted(patterns['themes'], key=lambda x: x['frequency'], reverse=True)[:3]
                context_parts.append(f"Частые темы: {[t['value'] for t in top_themes]}")
        
        if dreams:
            recent_summaries = []
            for dream in dreams[:5]:
                summary = f"- {dream.emotional_tone or 'нейтральный'} сон: {dream.themes or 'без темы'}"
                recent_summaries.append(summary)
            context_parts.append(f"Последние сны:\n" + "\n".join(recent_summaries))
        
        return "\n".join(context_parts) if context_parts else "Нет данных о снах"
