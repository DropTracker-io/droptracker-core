import asyncio
from datetime import datetime
from games.events.BoardGame import GielinorRace as BoardGame
from db.eventmodels import Event as EventModel, EventConfig, EventTeam, EventTeamInventory, EventParticipant, session
from enum import Enum

class EventType(Enum):
    BOARD_GAME = "BoardGame"


class Event:
    def __init__(self, group_id: int = -1, id: int = -1):
        """
        :param group_id: The ID of the group
        :param id: The ID of the event, if it already existed.
        """
        self.id = id
        self.group_id = group_id
        self.game_type = EventType.BOARD_GAME
        if self.id == -1:
            asyncio.run(self.create())
        else:
            self.game = BoardGame

    async def create(self) -> None:
        event = EventModel(name=f"Group {self.group_id}'s Event",
                           type=EventType.BOARD_GAME.value,
                           description="An Old School RuneScape event",
                           start_date=datetime.now(),
                           status="startup",
                           author_id=self.author_id)
        session.add(event)
        session.commit()
        self.id = event.id
        print(f"A new {self.type} event has been created in the database with id {self.id}")

    def game_loop(self) -> None:
        while True:
            winner = self.game.check_win_condition()
            if winner:
                break
            self.game.save_game_state()
    
    def check_task(self, player_id: str, item_name: str) -> bool:
        return self.game.check_task(player_id, item_name)