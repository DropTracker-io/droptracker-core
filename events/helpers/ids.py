from db.models import ItemList, Session, NpcList

def get_item_id(item_name: str) -> int:
    with Session() as session:
        item = session.query(ItemList).filter(ItemList.item_name == item_name).filter(ItemList.noted == False).first()
    if not item:
        print(f"Item {item_name} not found")
        return None
    return item.item_id

def get_npc_id(npc_name: str) -> int:
    with Session() as session:
        npc = session.query(NpcList).filter(NpcList.npc_name == npc_name).first()
    if not npc:
        print(f"NPC {npc_name} not found")
        return None
    return npc.npc_id
