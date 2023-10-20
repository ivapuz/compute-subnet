# The MIT License (MIT)
# Copyright © 2023 GitPhantom

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
import igpu
import cpuinfo as cpuinfo

#The following function is responsible for providing gpu information
def gpu_info():
    try:
        #Count of existing gpus
        gpu_count = igpu.count_devices()
        
        #Get the detailed information for each gpu (name, capacity)
        gpu_details = []
        for index in range(gpu_count):
            print(f"The gpu{index}:")
            gpu = igpu.get_device(0)
            gpu_details.append({"capacity": gpu.memory.total})
        return {"count":gpu_count, "details": gpu_details}
    except Exception as e:
        print(f"An error occurred: {e}")
        return {"count":0}

#The following function is responsible for providing cpu information
def cpu_info():
    try:
        info = {}
    
        # Create an instance of the CpuInfo class
        cpu_info = cpuinfo.get_cpu_info()

        # Get various CPU details
        info["vendor_id_raw"] = cpu_info["vendor_id_raw"]
        info["brand_raw"] = cpu_info["brand_raw"]
        info["hz_advertised_friendly"] = cpu_info["hz_advertised_friendly"]
        info["arch"] = cpu_info["arch"]
        info["bits"] = cpu_info["bits"]
        info["count"] = cpu_info["count"]
        return info
    except Exception as e:
        print(f"An error occurred: {e}")
        return {"count":0}