"""
Example usage of the enhanced task system with BaseTask templates.

This file demonstrates how to create BaseTask templates that can be reused
across multiple events, and how to create EventTasks from those templates.
"""

from .TaskFactory import TaskFactory
from .AssignedTask import AssignedTask
from .TaskType import TaskType


def create_base_task_library():
    """Create a library of BaseTask templates for common tasks."""
    
    base_tasks = []
    
    # ==================== Item Collection Base Tasks ====================
    
    # Common item collection tasks
    base_tasks.append(TaskFactory.create_base_item_collection_task(
        name="Starter Gear Collection",
        points=25,
        required_items={
            "Iron Scimitar": 1,
            "Iron Chainbody": 1,
            "Iron Platelegs": 1,
            "Leather Boots": 1
        },
        description="Collect basic iron armor set",
        difficulty="easy"
    ))
    
    base_tasks.append(TaskFactory.create_base_item_collection_task(
        name="Rare Drop Hunt",
        points=100,
        required_items={
            "Dragon Chainbody": 1,
            "Draconic Visage": 1,
            "Abyssal Whip": 1
        },
        description="Collect valuable rare drops",
        difficulty="very_hard"
    ))
    
    # ==================== Boss KC Base Tasks ====================
    
    # Various boss kill count tasks
    base_tasks.append(TaskFactory.create_base_kc_target_task(
        name="Zulrah Apprentice",
        points=50,
        boss_name="Zulrah",
        target_kc=25,
        description="Learn the basics of Zulrah",
        difficulty="medium"
    ))
    
    base_tasks.append(TaskFactory.create_base_kc_target_task(
        name="Zulrah Master",
        points=150,
        boss_name="Zulrah",
        target_kc=100,
        description="Master the art of Zulrah",
        difficulty="hard"
    ))
    
    base_tasks.append(TaskFactory.create_base_kc_target_task(
        name="Vorkath Slayer",
        points=100,
        boss_name="Vorkath",
        target_kc=50,
        description="Defeat the undead dragon",
        difficulty="hard"
    ))
    
    base_tasks.append(TaskFactory.create_base_kc_target_task(
        name="Giant Mole Exterminator",
        points=30,
        boss_name="Giant Mole",
        target_kc=50,
        description="Clear out the mole infestation",
        difficulty="easy"
    ))
    
    # ==================== Skill XP Base Tasks ====================
    
    # Common skill targets
    base_tasks.append(TaskFactory.create_base_xp_target_task(
        name="Woodcutting Grind",
        points=75,
        skill_name="Woodcutting",
        target_xp=1000000,
        description="Gain 1M Woodcutting experience",
        difficulty="medium"
    ))
    
    base_tasks.append(TaskFactory.create_base_xp_target_task(
        name="Mining Marathon",
        points=100,
        skill_name="Mining",
        target_xp=2000000,
        description="Gain 2M Mining experience",
        difficulty="hard"
    ))
    
    base_tasks.append(TaskFactory.create_base_xp_target_task(
        name="Fishing Session",
        points=50,
        skill_name="Fishing",
        target_xp=500000,
        description="Gain 500K Fishing experience",
        difficulty="easy"
    ))
    
    # ==================== Efficiency Base Tasks ====================
    
    base_tasks.append(TaskFactory.create_base_ehp_target_task(
        name="Efficient Player",
        points=200,
        target_ehp=10.0,
        description="Achieve 10 hours of efficient gameplay",
        difficulty="hard"
    ))
    
    base_tasks.append(TaskFactory.create_base_ehb_target_task(
        name="Boss Efficiency Expert",
        points=150,
        target_ehb=5.0,
        description="Achieve 5 hours of efficient bossing",
        difficulty="hard"
    ))
    
    # ==================== Loot Value Base Tasks ====================
    
    base_tasks.append(TaskFactory.create_base_loot_value_task(
        name="Treasure Hunter",
        points=100,
        target_value=10000000,  # 10M GP
        description="Collect 10M GP worth of loot from any source",
        difficulty="medium"
    ))
    
    base_tasks.append(TaskFactory.create_base_loot_value_task(
        name="Vorkath Profit",
        points=75,
        target_value=5000000,  # 5M GP
        source_npc="Vorkath",
        description="Earn 5M GP from Vorkath kills",
        difficulty="medium"
    ))
    
    base_tasks.append(TaskFactory.create_base_loot_value_task(
        name="Zulrah Bank Maker",
        points=100,
        target_value=8000000,  # 8M GP
        source_npc="Zulrah",
        description="Earn 8M GP from Zulrah kills",
        difficulty="hard"
    ))
    
    # ==================== Custom Base Tasks ====================
    
    base_tasks.append(TaskFactory.create_base_custom_task(
        name="Quest Master",
        points=300,
        task_config={
            "quest_type": "grandmaster",
            "required_quests": 5,
            "quest_points_required": 150
        },
        description="Complete multiple grandmaster quests",
        difficulty="very_hard"
    ))
    
    base_tasks.append(TaskFactory.create_base_custom_task(
        name="Achievement Diary Elite",
        points=250,
        task_config={
            "diary_difficulty": "elite",
            "required_diaries": 1,
            "specific_diary": "Western Provinces"
        },
        description="Complete an Elite Achievement Diary",
        difficulty="very_hard"
    ))
    
    return base_tasks


def demonstrate_base_task_usage():
    """Show how to use BaseTask templates to create EventTasks."""
    
    print("=== Creating Base Task Library ===")
    base_tasks = create_base_task_library()
    print(f"Created {len(base_tasks)} base task templates")
    
    print("\n=== Base Task Previews ===")
    for task in base_tasks[:5]:  # Show first 5
        print(f"- {task.name} ({task.difficulty}): {task.get_preview_text()}")
    
    print("\n=== Creating Event Tasks from Base Tasks ===")
    
    # Simulate creating tasks for a new event
    event_id = 123
    
    # Example 1: Use base task as-is
    zulrah_base = next(task for task in base_tasks if task.name == "Zulrah Apprentice")
    zulrah_event_task = zulrah_base.to_event_task(event_id)
    print(f"Created EventTask: {zulrah_event_task.name} (Event {event_id}) - {zulrah_event_task.points} points")
    
    # Example 2: Override points when creating EventTask
    mining_base = next(task for task in base_tasks if task.name == "Mining Marathon")
    mining_event_task = mining_base.to_event_task(event_id, points_override=150)  # Changed from 100 to 150
    print(f"Created EventTask: {mining_event_task.name} (Event {event_id}) - {mining_event_task.points} points (overridden)")
    
    # Example 3: Using TaskFactory utility method
    treasure_base = next(task for task in base_tasks if task.name == "Treasure Hunter")
    treasure_event_task = TaskFactory.create_event_task_from_base(treasure_base, event_id)
    print(f"Created EventTask: {treasure_event_task.name} (Event {event_id}) - {treasure_event_task.points} points")
    
    return [zulrah_event_task, mining_event_task, treasure_event_task]


def show_base_task_management_workflow():
    """Demonstrate a complete workflow for managing base tasks."""
    
    print("=== Base Task Management Workflow ===\n")
    
    # Step 1: Create base tasks for common scenarios
    print("1. Creating base tasks for different difficulty levels:")
    
    # Easy tier tasks
    easy_tasks = [
        TaskFactory.create_base_kc_target_task(
            name="Chaos Elemental Beginner",
            points=20,
            boss_name="Chaos Elemental",
            target_kc=10,
            difficulty="easy"
        ),
        TaskFactory.create_base_xp_target_task(
            name="Firemaking Starter",
            points=15,
            skill_name="Firemaking",
            target_xp=100000,
            difficulty="easy"
        )
    ]
    
    # Medium tier tasks
    medium_tasks = [
        TaskFactory.create_base_kc_target_task(
            name="Barrows Brothers Challenge",
            points=75,
            boss_name="Barrows Brothers",
            target_kc=100,
            difficulty="medium"
        ),
        TaskFactory.create_base_loot_value_task(
            name="Medium Value Hunt",
            points=60,
            target_value=3000000,
            difficulty="medium"
        )
    ]
    
    # Hard tier tasks
    hard_tasks = [
        TaskFactory.create_base_kc_target_task(
            name="Theatre of Blood Completer",
            points=200,
            boss_name="Theatre of Blood",
            target_kc=25,
            difficulty="hard"
        ),
        TaskFactory.create_base_ehp_target_task(
            name="Efficiency Expert",
            points=250,
            target_ehp=15.0,
            difficulty="hard"
        )
    ]
    
    all_base_tasks = easy_tasks + medium_tasks + hard_tasks
    
    for difficulty, tasks in [("Easy", easy_tasks), ("Medium", medium_tasks), ("Hard", hard_tasks)]:
        print(f"  {difficulty} Tasks:")
        for task in tasks:
            print(f"    - {task.name}: {task.get_preview_text()}")
    
    print(f"\n2. Base task library now contains {len(all_base_tasks)} templates")
    
    # Step 3: Create event-specific tasks
    print("\n3. Creating tasks for Event #456:")
    event_tasks = []
    
    # Use some base tasks as-is
    for base_task in all_base_tasks[:3]:
        event_task = base_task.to_event_task(456)
        event_tasks.append(event_task)
        print(f"   Added: {event_task.name} ({event_task.points} pts)")
    
    # Use some with point modifications
    for base_task in all_base_tasks[3:5]:
        modified_points = int(base_task.points * 1.5)  # 50% bonus for this event
        event_task = base_task.to_event_task(456, points_override=modified_points)
        event_tasks.append(event_task)
        print(f"   Added: {event_task.name} ({modified_points} pts - bonus applied)")
    
    print(f"\n4. Event #456 now has {len(event_tasks)} tasks ready to be assigned to teams")
    
    return all_base_tasks, event_tasks


def show_task_filtering_examples():
    """Show how to filter and organize base tasks."""
    
    print("=== Task Filtering Examples ===\n")
    
    # Create a diverse set of base tasks
    base_tasks = create_base_task_library()
    
    # Group by difficulty
    by_difficulty = {}
    for task in base_tasks:
        difficulty = task.difficulty or "unknown"
        if difficulty not in by_difficulty:
            by_difficulty[difficulty] = []
        by_difficulty[difficulty].append(task)
    
    print("Tasks grouped by difficulty:")
    for difficulty, tasks in by_difficulty.items():
        print(f"  {difficulty.capitalize()}: {len(tasks)} tasks")
        for task in tasks[:2]:  # Show first 2 of each difficulty
            print(f"    - {task.name}")
    
    # Group by task type
    by_type = {}
    for task in base_tasks:
        task_type = task.task_type.value
        if task_type not in by_type:
            by_type[task_type] = []
        by_type[task_type].append(task)
    
    print(f"\nTasks grouped by type:")
    for task_type, tasks in by_type.items():
        print(f"  {task_type}: {len(tasks)} tasks")
    
    # Show point distribution
    point_ranges = {"Low (≤50)": [], "Medium (51-100)": [], "High (>100)": []}
    for task in base_tasks:
        if task.points <= 50:
            point_ranges["Low (≤50)"].append(task)
        elif task.points <= 100:
            point_ranges["Medium (51-100)"].append(task)
        else:
            point_ranges["High (>100)"].append(task)
    
    print(f"\nTasks grouped by point value:")
    for range_name, tasks in point_ranges.items():
        print(f"  {range_name}: {len(tasks)} tasks")


if __name__ == "__main__":
    print("=" * 60)
    print("ENHANCED TASK SYSTEM WITH BASE TASK TEMPLATES")
    print("=" * 60)
    
    # Run all examples
    demonstrate_base_task_usage()
    
    print("\n" + "="*60)
    show_base_task_management_workflow()
    
    print("\n" + "="*60)
    show_task_filtering_examples()
    
    print("\n" + "="*60)
    print("SUMMARY:")
    print("- BaseTask: Templates that can be reused across events")
    print("- EventTask: Actual tasks for specific events (created from BaseTask)")
    print("- TaskFactory: Convenient creation methods for both types")
    print("- Easy conversion from BaseTask → EventTask with optional point overrides")
    print("=" * 60) 