from typing import Dict, Any, Optional, List
from .EventTask import EventTask, TaskType
from .BaseTask import BaseTask


class TaskFactory:
    """Factory class for creating different types of tasks with proper configuration."""
    
    # ==================== EventTask Creation Methods ====================
    
    @staticmethod
    def create_item_collection_task(
        event_id: int,
        name: str,
        points: int,
        required_items: Dict[str, int],
        description: Optional[str] = None,
        difficulty: Optional[str] = None
    ) -> EventTask:
        """
        Create an item collection task.
        
        Args:
            event_id: ID of the event
            name: Task name
            points: Point value
            required_items: Dict of item_name -> quantity
            description: Optional description
            difficulty: Optional difficulty level
        """
        return EventTask(
            event_id=event_id,
            name=name,
            points=points,
            task_type=TaskType.ITEM_COLLECTION,
            description=description,
            difficulty=difficulty,
            required_items=required_items
        )
    
    @staticmethod
    def create_kc_target_task(
        event_id: int,
        name: str,
        points: int,
        source_npcs: List[str],
        target_kc: int,
        description: Optional[str] = None,
        difficulty: Optional[str] = None
    ) -> EventTask:
        """
        Create a KC (kill count) target task.
        
        Args:
            event_id: ID of the event
            name: Task name
            points: Point value
            source_npcs: Name of the boss/monster
            target_kc: Target kill count
            description: Optional description
            difficulty: Optional difficulty level
        """
        task_config = {
            "source_npcs": source_npcs,
            "target_kc": target_kc
        }
        
        return EventTask(
            event_id=event_id,
            name=name,
            points=points,
            task_type=TaskType.KC_TARGET,
            description=description,
            difficulty=difficulty,
            task_config=task_config
        )
    
    @staticmethod
    def create_xp_target_task(
        event_id: int,
        name: str,
        points: int,
        skill_name: str,
        target_xp: int,
        description: Optional[str] = None,
        difficulty: Optional[str] = None
    ) -> EventTask:
        """
        Create an XP target task.
        
        Args:
            event_id: ID of the event
            name: Task name
            points: Point value
            skill_name: Name of the skill
            target_xp: Target XP amount
            description: Optional description
            difficulty: Optional difficulty level
        """
        task_config = {
            "skill_name": skill_name,
            "target_xp": target_xp
        }
        
        return EventTask(
            event_id=event_id,
            name=name,
            points=points,
            task_type=TaskType.XP_TARGET,
            description=description,
            difficulty=difficulty,
            task_config=task_config
        )
    
    @staticmethod
    def create_ehp_target_task(
        event_id: int,
        name: str,
        points: int,
        target_ehp: float,
        description: Optional[str] = None,
        difficulty: Optional[str] = None
    ) -> EventTask:
        """
        Create an EHP (Efficient Hours Played) target task.
        
        Args:
            event_id: ID of the event
            name: Task name
            points: Point value
            target_ehp: Target EHP amount
            description: Optional description
            difficulty: Optional difficulty level
        """
        task_config = {
            "target_ehp": target_ehp
        }
        
        return EventTask(
            event_id=event_id,
            name=name,
            points=points,
            task_type=TaskType.EHP_TARGET,
            description=description,
            difficulty=difficulty,
            task_config=task_config
        )
    
    @staticmethod
    def create_ehb_target_task(
        event_id: int,
        name: str,
        points: int,
        target_ehb: float,
        description: Optional[str] = None,
        difficulty: Optional[str] = None
    ) -> EventTask:
        """
        Create an EHB (Efficient Hours Bossed) target task.
        
        Args:
            event_id: ID of the event
            name: Task name
            points: Point value
            target_ehb: Target EHB amount
            description: Optional description
            difficulty: Optional difficulty level
        """
        task_config = {
            "target_ehb": target_ehb
        }
        
        return EventTask(
            event_id=event_id,
            name=name,
            points=points,
            task_type=TaskType.EHB_TARGET,
            description=description,
            difficulty=difficulty,
            task_config=task_config
        )
    
    @staticmethod
    def create_loot_value_task(
        event_id: int,
        name: str,
        points: int,
        target_value: int,
        source_npcs: Optional[List[str]] = None,
        description: Optional[str] = None,
        difficulty: Optional[str] = None
    ) -> EventTask:
        """
        Create a loot value collection task.
        
        Args:
            event_id: ID of the event
            name: Task name
            points: Point value
            target_value: Target loot value in GP
            source_npcs: Optional specific NPC/boss (if None, any source counts)
            description: Optional description
            difficulty: Optional difficulty level
        """
        task_config = {
            "target_value": target_value
        }
        
        if source_npcs:
            task_config["source_npcs"] = source_npcs
        
        return EventTask(
            event_id=event_id,
            name=name,
            points=points,
            task_type=TaskType.LOOT_VALUE,
            description=description,
            difficulty=difficulty,
            task_config=task_config
        )
    
    @staticmethod
    def create_custom_task(
        event_id: int,
        name: str,
        points: int,
        task_config: Dict[str, Any],
        description: Optional[str] = None,
        difficulty: Optional[str] = None
    ) -> EventTask:
        """
        Create a custom task with flexible configuration.
        
        Args:
            event_id: ID of the event
            name: Task name
            points: Point value
            task_config: Custom configuration dictionary
            description: Optional description
            difficulty: Optional difficulty level
        """
        return EventTask(
            event_id=event_id,
            name=name,
            points=points,
            task_type=TaskType.CUSTOM,
            description=description,
            difficulty=difficulty,
            task_config=task_config
        )

    # ==================== BaseTask Template Creation Methods ====================
    
    @staticmethod
    def create_base_item_collection_task(
        name: str,
        points: int,
        required_items: Dict[str, int],
        description: Optional[str] = None,
        difficulty: Optional[str] = None,
        created_by: Optional[int] = None,
        task_config: Optional[Dict[str, Any]] = None
    ) -> BaseTask:
        """Create a base item collection task template."""
        return BaseTask(
            name=name,
            points=points,
            task_type=TaskType.ITEM_COLLECTION,
            description=description,
            difficulty=difficulty,
            required_items=required_items,
            created_by=created_by,
            task_config=task_config
        )
        
    
    @staticmethod
    def create_base_kc_target_task(
        name: str,
        points: int,
        source_npcs: List[str],
        target_kc: int,
        description: Optional[str] = None,
        difficulty: Optional[str] = None,
        created_by: Optional[int] = None
    ) -> BaseTask:
        """Create a base KC target task template."""
        task_config = {
            "source_npcs": source_npcs,
            "target_kc": target_kc
        }
        
        return BaseTask(
            name=name,
            points=points,
            task_type=TaskType.KC_TARGET,
            description=description,
            difficulty=difficulty,
            task_config=task_config,
            created_by=created_by
        )
    
    @staticmethod
    def create_base_xp_target_task(
        name: str,
        points: int,
        skill_name: str,
        target_xp: int,
        description: Optional[str] = None,
        difficulty: Optional[str] = None,
        created_by: Optional[int] = None
    ) -> BaseTask:
        """Create a base XP target task template."""
        task_config = {
            "skill_name": skill_name,
            "target_xp": target_xp
        }
        
        return BaseTask(
            name=name,
            points=points,
            task_type=TaskType.XP_TARGET,
            description=description,
            difficulty=difficulty,
            task_config=task_config,
            created_by=created_by
        )
    
    @staticmethod
    def create_base_ehp_target_task(
        name: str,
        points: int,
        target_ehp: float,
        description: Optional[str] = None,
        difficulty: Optional[str] = None,
        created_by: Optional[int] = None
    ) -> BaseTask:
        """Create a base EHP target task template."""
        task_config = {
            "target_ehp": target_ehp
        }
        
        return BaseTask(
            name=name,
            points=points,
            task_type=TaskType.EHP_TARGET,
            description=description,
            difficulty=difficulty,
            task_config=task_config,
            created_by=created_by
        )
    
    @staticmethod
    def create_base_ehb_target_task(
        name: str,
        points: int,
        target_ehb: float,
        description: Optional[str] = None,
        difficulty: Optional[str] = None,
        created_by: Optional[int] = None
    ) -> BaseTask:
        """Create a base EHB target task template."""
        task_config = {
            "target_ehb": target_ehb
        }
        
        return BaseTask(
            name=name,
            points=points,
            task_type=TaskType.EHB_TARGET,
            description=description,
            difficulty=difficulty,
            task_config=task_config,
            created_by=created_by
        )
    
    @staticmethod
    def create_base_loot_value_task(
        name: str,
        points: int,
        target_value: int,
        source_npcs: Optional[List[str]] = None,
        description: Optional[str] = None,
        difficulty: Optional[str] = None,
        created_by: Optional[int] = None
    ) -> BaseTask:
        """Create a base loot value task template."""
        task_config = {
            "target_value": target_value
        }
        
        if source_npcs:
            task_config["source_npcs"] = source_npcs
        
        return BaseTask(
            name=name,
            points=points,
            task_type=TaskType.LOOT_VALUE,
            description=description,
            difficulty=difficulty,
            task_config=task_config,
            created_by=created_by
        )
    
    @staticmethod
    def create_base_custom_task(
        name: str,
        points: int,
        task_config: Dict[str, Any],
        description: Optional[str] = None,
        difficulty: Optional[str] = None,
        created_by: Optional[int] = None
    ) -> BaseTask:
        """Create a base custom task template."""
        return BaseTask(
            name=name,
            points=points,
            task_type=TaskType.CUSTOM,
            description=description,
            difficulty=difficulty,
            task_config=task_config,
            created_by=created_by
        )
    
    # ==================== Utility Methods ====================
    
    @staticmethod
    def create_event_task_from_base(base_task: BaseTask, event_id: int, points_override: Optional[int] = None) -> EventTask:
        """
        Create an EventTask from a BaseTask template.
        
        Args:
            base_task: The BaseTask template to use
            event_id: ID of the event
            points_override: Optional override for points value
            
        Returns:
            New EventTask instance
        """
        return base_task.to_event_task(event_id, points_override) 