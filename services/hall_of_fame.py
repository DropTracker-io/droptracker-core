
# @Task.create(IntervalTrigger(minutes=30))
# async def update_pb_embeds():
#     await logger.log("access", "update_pb_embeds called", "update_pb_embeds")
    
#     enabled_hofs = session.query(GroupConfiguration).filter(
#         GroupConfiguration.config_key == 'create_pb_embeds',
#         GroupConfiguration.config_value == '1'
#     ).all()
    
#     for enabled_group in enabled_hofs:
#         group: Group = enabled_group.group
#         print("Group with enabled HOFs is named", group.group_name)
        
#         enabled_bosses_raw = session.query(GroupConfiguration).filter(
#             GroupConfiguration.group_id == group.group_id,
#             GroupConfiguration.config_key == 'personal_best_embed_boss_list'
#         ).first()
        
#         if not enabled_bosses_raw:
#             continue
            
#         if enabled_bosses_raw.long_value == "" or enabled_bosses_raw.long_value == None:
#             enabled_bosses = json.loads(enabled_bosses_raw.config_value)
#         else:
#             enabled_bosses = json.loads(enabled_bosses_raw.long_value)

#         max_entries = session.query(GroupConfiguration.config_value).filter(
#             GroupConfiguration.group_id == group.group_id,
#             GroupConfiguration.config_key == 'number_of_pbs_to_display'
#         ).first()
        
#         max_entries = int(max_entries.config_value) if max_entries else 3
        
#         channel_id = session.query(GroupConfiguration.config_value).filter(
#             GroupConfiguration.group_id == group.group_id,
#             GroupConfiguration.config_key == 'channel_id_to_send_pb_embeds'
#         ).first()
        
#         if not channel_id:
#             continue
            
#         channel_id = channel_id.config_value
        
#         try:
#             channel = await bot.fetch_channel(channel_id=int(channel_id), force=True)
#             if not channel:
#                 continue
                
            
#             # Fetch existing messages to identify which NPCs are already posted
#             npcs = {}
#             for boss in enabled_bosses:
#                 npc_id = session.query(NpcList.npc_id).filter(NpcList.npc_name == boss).first()
#                 if npc_id:
#                     npcs[npc_id[0]] = boss
                    
#             to_be_posted = set(npcs.keys())
#             print("Processing", len(npcs), "NPCs for", group.group_name + "'s hall of fame...")
#             # Process existing messages with proper rate limiting
#             for i, npc_id in enumerate(list(npcs.keys())):
#                 existing_message = session.query(GroupPersonalBestMessage).filter(
#                     GroupPersonalBestMessage.group_id == group.group_id, 
#                     GroupPersonalBestMessage.boss_name == npcs[npc_id]
#                 ).first()
                
#                 if existing_message:
#                     # Wait 6 seconds after every 4 messages
#                     if i > 0 and i % 4 == 0:
#                         await asyncio.sleep(7)
                    
#                     try:
#                         success, wait_for_rate_limit = await update_boss_pb_embed(bot, group.group_id, npc_id, from_submission=False)
#                         if success:
#                             # Only remove from to_be_posted if the update was successful
#                             if npc_id in to_be_posted:
#                                 to_be_posted.remove(npc_id)
#                         else:
#                             pass
                        
#                         # Small delay between each edit
#                         if wait_for_rate_limit:
#                             await asyncio.sleep(2)
                        
#                     except Exception as e:
#                         await asyncio.sleep(7)
            
#             # Wait before posting new messages
#             await asyncio.sleep(6)
            
#             # Post new messages with proper rate limiting
            
#             for i, npc_id in enumerate(to_be_posted):
#                 # Wait 6 seconds after every 4 messages
#                 if i > 0 and i % 4 == 0:
#                     await asyncio.sleep(7)
                
#                 try:
#                     pb_embed = await create_boss_pb_embed(group.group_id, npcs[npc_id], max_entries)
#                     next_update = datetime.now() + timedelta(minutes=30)
#                     future_timestamp = int(time.mktime(next_update.timetuple()))
#                     now = datetime.now()
#                     now_timestamp = int(time.mktime(now.timetuple()))
#                     pb_embed.add_field(name="Next refresh:", value=f"<t:{future_timestamp}:R> (last: <t:{now_timestamp}:R>)", inline=False)
#                     posted_message = await channel.send(embed=pb_embed)
                    
#                     new_entry = GroupPersonalBestMessage(
#                         group_id=group.group_id, 
#                         boss_name=npcs[npc_id], 
#                         message_id=posted_message.id, 
#                         channel_id=channel.id
#                     )
#                     session.add(new_entry)
#                     session.commit()
                    
#                     # Small delay between each post
#                     await asyncio.sleep(1)
                    
#                 except Exception as e:
#                     print(f"Error posting message for {npcs[npc_id]}: {e}")
#                     await asyncio.sleep(7)
                    
#         except Exception as e:
#             print(f"Error processing channel {channel_id} for group {group.group_name}: {e}")
            
#         # Wait between processing different groups
#         await asyncio.sleep(7)
