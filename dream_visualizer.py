"""
Модуль визуализации снов через Gemini API
"""
import google.generativeai as genai
from config import GEMINI_API_KEY, GEMINI_MODEL
from typing import Dict, Optional
import os


class DreamVisualizer:
    """Класс для создания визуализаций снов"""
    
    def __init__(self):
        if GEMINI_API_KEY:
            genai.configure(api_key=GEMINI_API_KEY)
            self.model = genai.GenerativeModel(GEMINI_MODEL)
        else:
            self.model = None
    
    def generate_visualization_prompt(self, dream_analysis: Dict, dream_text: str) -> str:
        """
        Создаёт промпт для генерации изображения на основе анализа сна
        
        Args:
            dream_analysis: Результат анализа сна
            dream_text: Исходный текст сна
        
        Returns:
            Промпт для генерации изображения
        """
        # Извлекаем ключевые визуальные элементы
        locations = ", ".join(dream_analysis.get("locations", [])[:3])
        characters = ", ".join(dream_analysis.get("characters", [])[:3])
        symbols = ", ".join([s.get("symbol", s) if isinstance(s, dict) else s for s in dream_analysis.get("symbols", [])[:3]])
        emotional_tone = dream_analysis.get("emotional_tone", "нейтральная")
        
        prompt_parts = [
            "Создай художественное изображение сновидения со следующими элементами:",
            f"Эмоциональный тон: {emotional_tone}",
        ]
        
        if locations:
            prompt_parts.append(f"Места: {locations}")
        if characters:
            prompt_parts.append(f"Персонажи: {characters}")
        if symbols:
            prompt_parts.append(f"Символы: {symbols}")
        
        prompt_parts.append("Стиль: сюрреалистический, мистический, атмосферный. Цветовая палитра должна отражать эмоциональный тон сна.")
        
        return " ".join(prompt_parts)
    
    def generate_image_url(self, dream_analysis: Dict, dream_text: str) -> Optional[str]:
        """
        Генерирует изображение сна через Gemini
        
        Note: Gemini API не поддерживает генерацию изображений напрямую.
        Эта функция может быть использована для создания промпта для других сервисов
        или для интеграции с другими API генерации изображений (DALL-E, Midjourney и т.д.)
        
        Args:
            dream_analysis: Результат анализа сна
            dream_text: Исходный текст сна
        
        Returns:
            URL изображения или None
        """
        if not self.model:
            return None
        
        try:
            prompt = self.generate_visualization_prompt(dream_analysis, dream_text)
            
            # Примечание: Gemini API не генерирует изображения напрямую
            # Для генерации изображений можно использовать:
            # 1. OpenAI DALL-E API
            # 2. Stable Diffusion API
            # 3. Midjourney API
            
            # В этом примере возвращаем промпт, который можно использовать
            # для других сервисов генерации изображений
            
            # TODO: Интегрировать с сервисом генерации изображений
            # Например, DALL-E:
            # from openai import OpenAI
            # client = OpenAI(api_key=OPENAI_API_KEY)
            # response = client.images.generate(
            #     model="dall-e-3",
            #     prompt=prompt,
            #     size="1024x1024",
            #     quality="standard",
            #     n=1,
            # )
            # return response.data[0].url
            
            return None
            
        except Exception as e:
            print(f"Ошибка генерации изображения: {e}")
            return None
    
    def generate_with_dalle(self, dream_analysis: Dict, dream_text: str, openai_api_key: str) -> Optional[str]:
        """
        Генерирует изображение через DALL-E API (альтернативный метод)
        
        Args:
            dream_analysis: Результат анализа сна
            dream_text: Исходный текст сна
            openai_api_key: API ключ OpenAI
        
        Returns:
            URL изображения или None
        """
        try:
            from openai import OpenAI
            
            client = OpenAI(api_key=openai_api_key)
            prompt = self.generate_visualization_prompt(dream_analysis, dream_text)
            
            response = client.images.generate(
                model="dall-e-3",
                prompt=prompt,
                size="1024x1024",
                quality="standard",
                n=1,
            )
            
            return response.data[0].url
            
        except Exception as e:
            print(f"Ошибка генерации изображения через DALL-E: {e}")
            return None
