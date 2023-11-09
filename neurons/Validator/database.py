# The MIT License (MIT)
# Copyright © 2023 Crazydevlegend

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
# Step 1: Import necessary libraries and modules

import json
import sqlite3
import bittensor as bt

# Connect to the database (or create it if it doesn't exist)
conn = sqlite3.connect('database.db')

# Create a cursor
cursor = conn.cursor()

# Create a table
cursor.execute('CREATE TABLE IF NOT EXISTS miner_details (id INTEGER PRIMARY KEY, hotkey TEXT, details TEXT)')

#Fetch hotkeys from database that meets device_requirement
def select_miners_hotkey(device_requirement):
    #Fetch all records from miner_details table
    cursor.execute("SELECT * FROM miner_details")
    rows = cursor.fetchall()

    #Check if the miner meets device_requirement
    hotkey_list = []
    for row in rows:
        details = json.loads(row[2])
        if check_if_miner_meet(details, device_requirement) == True:
            hotkey_list.append(row[1])
    return hotkey_list

#Update the miner_details with perfInfo
def update(hotkey_list, perfInfo_responses):
    cursor.execute(f'DELETE FROM miner_details')
    for index, perfInfo in enumerate(perfInfo_responses):
        cursor.execute("INSERT INTO miner_details (hotkey, details) VALUES (?, ?)", (hotkey_list[index], json.dumps(perfInfo)))

#Check if the miner meets required details
def check_if_miner_meet(details, required_details):
    #CPU side
    cpu_miner = details['cpu']
    required_cpu = required_details['cpu']
    if cpu_miner['count'] < required_cpu['count']:
        return False
    
    #GPU side
    gpu_miner = details['gpu']
    required_gpu = required_details['gpu']
    if required_gpu and gpu_miner['capacity'] < required_gpu['capacity']:
        return False

    #Hard disk side
    hard_disk_miner = details['hard_disk']
    required_hard_disk = required_details['hard_disk']
    if hard_disk_miner['free'] < required_hard_disk['capacity']:
        return False

    #Ram side
    ram_miner = details['ram']
    required_ram = required_details['ram']
    if ram_miner['available'] < required_ram['capacity']:
        return False
    return True