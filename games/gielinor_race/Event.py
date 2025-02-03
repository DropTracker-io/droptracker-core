from datetime import datetime
from typing import List, Optional
from .GielinorRace import GielinorRace

class Event:
    def __init__(self, group_id: int, group_name: str, board_size: int = 100):
        self.group_id = group_id
        self.group_name = group_name
        self.start_date = datetime.now()
        self.end_date: Optional[datetime] = None
        self.winner: Optional[str] = None
        self.game = GielinorRace(group_id, board_size)

    @property
    def is_active(self) -> bool:
        return self.end_date is None

    @property
    def board_size(self) -> int:
        return self.game.board_size

    @property
    def teams(self) -> List[dict]:
        return [
            {
                'name': team.name,
                'players': [player.player_name for player in team.players],
                'position': team.position,
                'points': team.points
            } for team in self.game.teams
        ]

    @property
    def shop_items(self) -> List[dict]:
        return [
            {
                'name': item.name,
                'cost': item.cost,
                'effect': item.effect,
                'item_type': item.item_type.name
            } for item in self.game.shop.values()
        ]

    def end_game(self):
        self.end_date = datetime.now()
        winning_team = max(self.game.teams, key=lambda team: team.points)
        self.winner = winning_team.name

    def to_dict(self) -> dict:
        return {
            'group_id': self.group_id,
            'group_name': self.group_name,
            'start_date': self.start_date.strftime('%Y-%m-%d %H:%M:%S'),
            'end_date': self.end_date.strftime('%Y-%m-%d %H:%M:%S') if self.end_date else None,
            'winner': self.winner,
            'is_active': self.is_active,
            'board_size': self.board_size,
            'teams': self.teams,
            'shop_items': self.shop_items
        }
