import math

# Pre-calculate the experience required for each level for efficiency
# This avoids recalculating the sum every time the function is called.
# OSRS levels go up to 99, but we can extend it if needed for custom servers.
MAX_OSRS_LEVEL = 99
OSRS_LEVEL_XP_REQUIREMENTS = [0] * (MAX_OSRS_LEVEL + 1) # Index 0 is unused, index 1 is Level 1, etc.

# Calculate XP for each level based on the OSRS formula
current_total_xp = 0
for level_index in range(1, MAX_OSRS_LEVEL + 1):

    if level_index > 1:
        
        points_for_this_level_step = math.floor((level_index - 1) + 300.0 * math.pow(2.0, (level_index - 1) / 7.0))
        current_total_xp += math.floor(points_for_this_level_step / 4)
    
    OSRS_LEVEL_XP_REQUIREMENTS[level_index] = int(current_total_xp)

OSRS_LEVEL_XP_REQUIREMENTS[MAX_OSRS_LEVEL] = 13034431 
OSRS_LEVEL_XP_REQUIREMENTS.append(float('inf')) 


def get_osrs_level(experience: int) -> int:
    """
    Calculates the current Old School RuneScape level based on a provided amount of experience.

    Args:
        experience (int): The total amount of experience gained.

    Returns:
        int: The calculated current OSRS level (1-99). Returns 1 for negative XP.
             Returns 99 for any experience greater than or equal to the XP needed for level 99.
    """
    if experience < 0:
        return 1  

    if experience < OSRS_LEVEL_XP_REQUIREMENTS[2]: # If less than XP for level 2 (83 XP)
        return 1
    for level in range(2, MAX_OSRS_LEVEL + 1):
        if experience < OSRS_LEVEL_XP_REQUIREMENTS[level + 1]:
            return level
    return MAX_OSRS_LEVEL # Returns 99
