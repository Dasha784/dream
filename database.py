"""
Модуль работы с базой данных для хранения снов и анализов
"""
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Float, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from datetime import datetime
from typing import Optional, List, Dict
import json

Base = declarative_base()


class Dream(Base):
    """Модель сна"""
    __tablename__ = "dreams"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    dream_text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Результаты анализа
    emotions = Column(JSON)  # Список эмоций с весами
    locations = Column(JSON)  # Места
    characters = Column(JSON)  # Персонажи
    actions = Column(JSON)  # Действия
    symbols = Column(JSON)  # Символы
    archetypes = Column(JSON)  # Архетипы
    emotional_tone = Column(String)  # Основной эмоциональный тон
    themes = Column(JSON)  # Темы сна
    interpretation = Column(Text)  # Интерпретация
    interpretation_type = Column(String)  # Тип интерпретации
    advice = Column(Text)  # Совет на день
    lesson = Column(Text)  # Урок сна
    visualization_url = Column(String)  # URL изображения (если есть)


class DreamPattern(Base):
    """Паттерны в снах пользователя"""
    __tablename__ = "dream_patterns"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    pattern_type = Column(String)  # theme, emotion, symbol, archetype
    pattern_value = Column(String)  # Название паттерна
    frequency = Column(Integer, default=1)
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow)


class UserQuestion(Base):
    """Вопросы пользователя о снах"""
    __tablename__ = "user_questions"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    question = Column(Text, nullable=False)
    answer = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class Database:
    """Класс для работы с базой данных"""
    
    def __init__(self, database_url: str):
        self.engine = create_engine(database_url, echo=False)
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
    
    def get_session(self) -> Session:
        """Получить сессию базы данных"""
        return self.SessionLocal()
    
    def save_dream(self, user_id: int, dream_text: str, analysis: Dict) -> Dream:
        """Сохранить сон с анализом"""
        session = self.get_session()
        try:
            dream = Dream(
                user_id=user_id,
                dream_text=dream_text,
                emotions=analysis.get("emotions"),
                locations=analysis.get("locations"),
                characters=analysis.get("characters"),
                actions=analysis.get("actions"),
                symbols=analysis.get("symbols"),
                archetypes=analysis.get("archetypes"),
                emotional_tone=analysis.get("emotional_tone"),
                themes=analysis.get("themes"),
                interpretation=analysis.get("interpretation"),
                interpretation_type=analysis.get("interpretation_type"),
                advice=analysis.get("advice"),
                lesson=analysis.get("lesson"),
                visualization_url=analysis.get("visualization_url")
            )
            session.add(dream)
            session.commit()
            session.refresh(dream)
            
            # Обновляем паттерны
            self._update_patterns(user_id, analysis)
            
            return dream
        finally:
            session.close()
    
    def _update_patterns(self, user_id: int, analysis: Dict):
        """Обновить паттерны пользователя"""
        session = self.get_session()
        try:
            # Обновляем паттерны тем
            for theme in analysis.get("themes", []):
                pattern = session.query(DreamPattern).filter_by(
                    user_id=user_id,
                    pattern_type="theme",
                    pattern_value=theme
                ).first()
                if pattern:
                    pattern.frequency += 1
                    pattern.last_seen = datetime.utcnow()
                else:
                    session.add(DreamPattern(
                        user_id=user_id,
                        pattern_type="theme",
                        pattern_value=theme
                    ))
            
            # Обновляем эмоциональные паттерны
            if analysis.get("emotional_tone"):
                pattern = session.query(DreamPattern).filter_by(
                    user_id=user_id,
                    pattern_type="emotion",
                    pattern_value=analysis["emotional_tone"]
                ).first()
                if pattern:
                    pattern.frequency += 1
                    pattern.last_seen = datetime.utcnow()
                else:
                    session.add(DreamPattern(
                        user_id=user_id,
                        pattern_type="emotion",
                        pattern_value=analysis["emotional_tone"]
                    ))
            
            # Обновляем архетипы
            for archetype in analysis.get("archetypes", []):
                pattern = session.query(DreamPattern).filter_by(
                    user_id=user_id,
                    pattern_type="archetype",
                    pattern_value=archetype
                ).first()
                if pattern:
                    pattern.frequency += 1
                    pattern.last_seen = datetime.utcnow()
                else:
                    session.add(DreamPattern(
                        user_id=user_id,
                        pattern_type="archetype",
                        pattern_value=archetype
                    ))
            
            session.commit()
        finally:
            session.close()
    
    def get_user_dreams(self, user_id: int, limit: Optional[int] = None) -> List[Dream]:
        """Получить сны пользователя"""
        session = self.get_session()
        try:
            query = session.query(Dream).filter_by(user_id=user_id).order_by(Dream.created_at.desc())
            if limit:
                query = query.limit(limit)
            return query.all()
        finally:
            session.close()
    
    def get_user_patterns(self, user_id: int) -> Dict[str, List[Dict]]:
        """Получить паттерны пользователя"""
        session = self.get_session()
        try:
            patterns = session.query(DreamPattern).filter_by(user_id=user_id).all()
            result = {
                "themes": [],
                "emotions": [],
                "archetypes": [],
                "symbols": []
            }
            for pattern in patterns:
                if pattern.pattern_type in result:
                    result[pattern.pattern_type].append({
                        "value": pattern.pattern_value,
                        "frequency": pattern.frequency,
                        "first_seen": pattern.first_seen,
                        "last_seen": pattern.last_seen
                    })
            return result
        finally:
            session.close()
    
    def get_dream_statistics(self, user_id: int) -> Dict:
        """Получить статистику по снам пользователя"""
        dreams = self.get_user_dreams(user_id)
        if not dreams:
            return {}
        
        total_dreams = len(dreams)
        emotions_count = {}
        themes_count = {}
        archetypes_count = {}
        
        for dream in dreams:
            if dream.emotional_tone:
                emotions_count[dream.emotional_tone] = emotions_count.get(dream.emotional_tone, 0) + 1
            if dream.themes:
                for theme in dream.themes:
                    themes_count[theme] = themes_count.get(theme, 0) + 1
            if dream.archetypes:
                for archetype in dream.archetypes:
                    archetypes_count[archetype] = archetypes_count.get(archetype, 0) + 1
        
        return {
            "total_dreams": total_dreams,
            "emotions_distribution": emotions_count,
            "themes_distribution": themes_count,
            "archetypes_distribution": archetypes_count,
            "first_dream": dreams[-1].created_at if dreams else None,
            "last_dream": dreams[0].created_at if dreams else None
        }
    
    def save_question(self, user_id: int, question: str, answer: str):
        """Сохранить вопрос и ответ"""
        session = self.get_session()
        try:
            user_question = UserQuestion(
                user_id=user_id,
                question=question,
                answer=answer
            )
            session.add(user_question)
            session.commit()
        finally:
            session.close()
