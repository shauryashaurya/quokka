import pickle
import os
os.environ["POLAR_MAX_THREADS"] = "1"
import polars
import pandas as pd
os.environ['ARROW_DEFAULT_MEMORY_POOL'] = 'system'

import pyarrow as pa

import time
import numpy as np
import os, psutil
import boto3
import gc
import pyarrow.csv as csv
from io import StringIO, BytesIO
from state import PersistentStateVariable
from collections import deque
import pyarrow.compute as compute
import random

class Executor:
    def __init__(self) -> None:
        raise NotImplementedError
    def initialize(datasets):
        pass
    def set_early_termination(self):
        self.early_termination = True
    def execute(self,batch,stream_id, executor_id):
        raise NotImplementedError
    def done(self,executor_id):
        raise NotImplementedError    

class OutputCSVExecutor(Executor):
    def __init__(self, bucket, prefix, output_line_limit = 1000000) -> None:
        self.num = 0
        self.num_states = 0

        self.bucket = bucket
        self.prefix = prefix
        self.s3_resource = None
        self.output_line_limit = output_line_limit
        self.name = 0
        self.my_batches = deque()

    def serialize(self):
        pass
    
    def serialize(self, s):
        pass

    def execute(self,batches,stream_id, executor_id):

        if self.s3_resource is None:
            self.s3_resource = boto3.resource('s3')

        #self.num += 1
        self.my_batches.extend([i for i in batches if i is not None])
        
        curr_len = 0
        i = 0
        while i < len(self.my_batches):
            curr_len += len(self.my_batches[i])
            i += 1
            if curr_len > self.output_line_limit:
                da = BytesIO()
                csv.write_csv(polars.concat([self.my_batches.popleft() for k in range(i)]).to_arrow(), da, write_options = csv.WriteOptions(include_header=False))
                self.s3_resource.Object(self.bucket,self.prefix + "-" + str(executor_id) + "-" + str(self.name) + ".csv").put(Body=da.getvalue())
                self.name += 1
                i = 0
                curr_len = 0


    def done(self,executor_id):
        if len(self.my_batches) > 0:
            da = BytesIO()
            csv.write_csv(polars.concat(list(self.my_batches)).to_arrow(), da, write_options = csv.WriteOptions(include_header=False))
            self.s3_resource.Object(self.bucket,self.prefix + "-" + str(executor_id) + "-" + str(self.name) + ".csv").put(Body=da.getvalue())
            self.name += 1
        print("done")


class PolarJoinExecutor(Executor):
    # batch func here expects a list of dfs. This is a quark of the fact that join results could be a list of dfs.
    # batch func must return a list of dfs too
    def __init__(self, on = None, left_on = None, right_on = None, batch_func = None):

        # how many things you might checkpoint, the number of keys in the dict
        self.num_states = 2

        self.state0 = None
        self.state1 = None
        self.ckpt_start0 = 0
        self.ckpt_start1 = 0
        self.lengths = {0:0, 1:0}

        if on is not None:
            assert left_on is None and right_on is None
            self.left_on = on
            self.right_on = on
        else:
            assert left_on is not None and right_on is not None
            self.left_on = left_on
            self.right_on = right_on
        self.batch_func = batch_func
        # keys that will never be seen again, safe to delete from the state on the other side

    def serialize(self):
        result = {0:self.state0[self.ckpt_start0:] if (self.state0 is not None and len(self.state0[self.ckpt_start0:]) > 0) else None, 1:self.state1[self.ckpt_start1:] if (self.state1 is not None and len(self.state1[self.ckpt_start1:]) > 0) else None}
        if self.state0 is not None:
            self.ckpt_start0 = len(self.state0)
        if self.state1 is not None:
            self.ckpt_start1 = len(self.state1)
        return result, "inc"
    
    def deserialize(self, s):
        assert type(s) == list
        list0 = [i[0] for i in s if i[0] is not None]
        list1 = [i[1] for i in s if i[1] is not None]
        self.state0 = polars.concat(list0) if len(list0) > 0 else None
        self.state1 = polars.concat(list1) if len(list1) > 0 else None
        self.ckpt_start0 = len(self.state0) if self.state0 is not None else 0
        self.ckpt_start1 = len(self.state1) if self.state1 is not None else 0
    
    # the execute function signature does not change. stream_id will be a [0 - (length of InputStreams list - 1)] integer
    def execute(self,batches, stream_id, executor_id):
        # state compaction
        batch = polars.concat(batches)
        self.lengths[stream_id] += 1
        print("state", self.lengths)
        result = None
        if stream_id == 0:
            if self.state1 is not None:
                try:
                    result = batch.join(self.state1,left_on = self.left_on, right_on = self.right_on ,how='inner')
                except:
                    print(batch)
            if self.state0 is None:
                self.state0 = batch
            else:
                self.state0.vstack(batch, in_place = True)
             
        elif stream_id == 1:
            if self.state0 is not None:
                result = self.state0.join(batch,left_on = self.left_on, right_on = self.right_on ,how='inner')
            if self.state1 is None:
                self.state1 = batch
            else:
                self.state1.vstack(batch, in_place = True)
        
        if result is not None and len(result) > 0:
            if self.batch_func is not None:
                da =  self.batch_func(result.to_pandas())
                return da
            else:
                print("RESULT LENGTH",len(result))
                return result
    
    def done(self,executor_id):
        print(len(self.state0),len(self.state1))
        print("done join ", executor_id)


class Polar3JoinExecutor(Executor):
    # batch func here expects a list of dfs. This is a quark of the fact that join results could be a list of dfs.
    # batch func must return a list of dfs too
    def __init__(self, on = None, left_on = None, right_on = None, batch_func = None):
        self.state0 = None
        self.state1 = None
        self.lengths = {0:0, 1:0}

        if on is not None:
            assert left_on is None and right_on is None
            self.left_on = on
            self.right_on = on
        else:
            assert left_on is not None and right_on is not None
            self.left_on = left_on
            self.right_on = right_on
        self.batch_func = batch_func
        # keys that will never be seen again, safe to delete from the state on the other side

    def serialize(self):
        return pickle.dumps({"state0":self.state0, "state1":self.state1})
    
    def deserialize(self, s):
        stuff = pickle.loads(s)
        self.state0 = stuff["state0"]
        self.state1 = stuff["state1"]
    
    # the execute function signature does not change. stream_id will be a [0 - (length of InputStreams list - 1)] integer
    def execute(self,batches, stream_id, executor_id):
        # state compaction
        batch = polars.concat(batches)
        self.lengths[stream_id] += 1
        print("state", self.lengths)
        result = None
        if stream_id == 0:
            if self.state1 is not None:
                try:
                    result = batch.join(self.state1,left_on = self.left_on, right_on = self.right_on ,how='inner')
                except:
                    print(batch)
            if self.state0 is None:
                self.state0 = batch
            else:
                self.state0.vstack(batch, in_place = True)
             
        elif stream_id == 1:
            if self.state0 is not None:
                result = self.state0.join(batch,left_on = self.left_on, right_on = self.right_on ,how='inner')
            if self.state1 is None:
                self.state1 = batch
            else:
                self.state1.vstack(batch, in_place = True)
        
        if result is not None and len(result) > 0:
            if self.batch_func is not None:
                da =  self.batch_func(result.to_pandas())
                return da
            else:
                print("RESULT LENGTH",len(result))
                return result
    
    def done(self,executor_id):
        print(len(self.state0),len(self.state1))
        print("done join ", executor_id)


class OOCJoinExecutor(Executor):
    # batch func here expects a list of dfs. This is a quark of the fact that join results could be a list of dfs.
    # batch func must return a list of dfs too
    def __init__(self, on = None, left_on = None, right_on = None, left_primary = False, right_primary = False, batch_func = None):
        self.state0 = PersistentStateVariable()
        self.state1 = PersistentStateVariable()
        if on is not None:
            assert left_on is None and right_on is None
            self.left_on = on
            self.right_on = on
        else:
            assert left_on is not None and right_on is not None
            self.left_on = left_on
            self.right_on = right_on

        self.batch_func = batch_func

    # the execute function signature does not change. stream_id will be a [0 - (length of InputStreams list - 1)] integer
    def execute(self,batches, stream_id, executor_id):

        batch = pd.concat(batches)
        results = []

        if stream_id == 0:
            if len(self.state1) > 0:
                results = [batch.merge(i,left_on = self.left_on, right_on = self.right_on ,how='inner',suffixes=('_a','_b')) for i in self.state1]
            self.state0.append(batch)
             
        elif stream_id == 1:
            if len(self.state0) > 0:
                results = [i.merge(batch,left_on = self.left_on, right_on = self.right_on ,how='inner',suffixes=('_a','_b')) for i in self.state0]
            self.state1.append(batch)
        
        if len(results) > 0:
            if self.batch_func is not None:
                return self.batch_func(results)
            else:
                return results
    
    def done(self,executor_id):
        print("done join ", executor_id)

# WARNING: aggregation on index match! Not on column match
class AggExecutor(Executor):
    def __init__(self, fill_value = 0, final_func = None):

        # how many things you might checkpoint, the number of keys in the dict
        self.num_states = 1

        self.state = None
        self.fill_value = fill_value
        self.final_func = final_func

    def serialize(self):
        return {0:self.state}, "all"
    
    def deserialize(self, s):
        # the default is to get a list of dictionaries.
        assert type(s) == list and len(s) == 1
        self.state = s[0][0]
    
    # the execute function signature does not change. stream_id will be a [0 - (length of InputStreams list - 1)] integer
    def execute(self,batches, stream_id, executor_id):
        for batch in batches:
            assert type(batch) == pd.core.frame.DataFrame # polars add has no index, will have wierd behavior
            if self.state is None:
                self.state = batch 
            else:
                self.state = self.state.add(batch, fill_value = self.fill_value)
    
    def done(self,executor_id):
        print(self.state)
        if self.final_func:
            return self.final_func(self.state)
        else:
            return self.state

class LimitExecutor(Executor):
    def __init__(self, limit) -> None:
        self.limit = limit
        self.state = []

    def execute(self, batches, stream_id, executor_id):

        batch = pd.concat(batches)
        self.state.append(batch)
        length = sum([len(i) for i in self.state])
        if length > self.limit:
            self.set_early_termination()
    
    def done(self):
        return pd.concat(self.state)[:self.limit]

class CountExecutor(Executor):
    def __init__(self) -> None:

        # how many things you might checkpoint, the number of keys in the dict
        self.num_states = 1

        self.state = 0

    def execute(self, batches, stream_id, executor_id):
        
        self.state += sum(len(batch) for batch in batches)
    
    def serialize(self):
        return {0:self.state}, "all"
    
    def deserialize(self, s):
        # the default is to get a list of things 
        assert type(s) == list and len(s) == 1
        self.state = s[0][0]
    
    def done(self, executor_id):
        print("COUNT:", self.state)
        return polars.DataFrame([self.state])


class MergeSortedExecutor(Executor):
    def __init__(self, key, record_batch_rows = None, length_limit = 5000, file_prefix = "mergesort") -> None:
        self.num_states = 0
        self.states = []
        self.num = 1
        self.key = key
        self.record_batch_rows = record_batch_rows
        self.fileno = 0
        self.length_limit = length_limit
        self.prefix = file_prefix # make sure this is different for different executors

        self.data_dir = "/data"
    
    def serialize(self):
        return {}, "all" # don't support fault tolerance of sort
    
    def deserialize(self, s):
        raise Exception

    def write_out_df_to_disk(self, target_filepath, input_mem_table):
        arrow_table = input_mem_table.to_arrow()
        batches = arrow_table.to_batches(self.record_batch_rows)
        writer =  pa.ipc.new_file(pa.OSFile(target_filepath, 'wb'), arrow_table.schema)
        for batch in batches:
            writer.write(batch)
        writer.close()
    
    # with minimal memory used!
    def produce_sorted_file_from_two_sorted_files(self, target_filepath, input_filepath1, input_filepath2):

        read_time = 0
        sort_time = 0
        write_time = 0

        source1 =  pa.ipc.open_file(pa.memory_map(input_filepath1, 'rb'))
        number_of_batches_in_source1 = source1.num_record_batches
        source2 =  pa.ipc.open_file(pa.memory_map(input_filepath2, 'rb'))
        number_of_batches_in_source2 = source2.num_record_batches

        next_batch_to_get1 = 1

        start = time.time()
        cached_batches_in_mem1 = polars.from_arrow(pa.Table.from_batches([source1.get_batch(0)]))
        next_batch_to_get2 = 1
        cached_batches_in_mem2 = polars.from_arrow(pa.Table.from_batches([source2.get_batch(0)]))
        read_time += time.time() - start

        writer =  pa.ipc.new_file(pa.OSFile(target_filepath, 'wb'), source1.schema)

        # each iteration will write a batch to the target filepath
        while len(cached_batches_in_mem1) > 0 and len(cached_batches_in_mem2) > 0:
            
            disk_portion1 = cached_batches_in_mem1[:self.record_batch_rows]
            disk_portion1['asdasd'] = np.zeros(len(disk_portion1))

            disk_portion2 = cached_batches_in_mem2[:self.record_batch_rows]
            disk_portion2['asdasd'] = np.ones(len(disk_portion2))
            
            start = time.time()
            new_batch = polars.concat([disk_portion1, disk_portion2]).sort(self.key)[:self.record_batch_rows]
            sort_time += time.time() - start
            disk_contrib2 = int(new_batch['asdasd'].sum())
            disk_contrib1 = len(new_batch) - disk_contrib2
            new_batch = new_batch.drop('asdasd')

            #print(source.schema, new_batch.to_arrow().schema)
            start = time.time()
            writer.write(new_batch.to_arrow().to_batches()[0])
            write_time += time.time() - start

            cached_batches_in_mem1 = cached_batches_in_mem1[disk_contrib1:]
            
            start = time.time()
            if len(cached_batches_in_mem1) < self.record_batch_rows and next_batch_to_get1 < number_of_batches_in_source1:
                next_batch = source1.get_batch(next_batch_to_get1)
                next_batch_to_get1 += 1
                next_batch = polars.from_arrow(pa.Table.from_batches([next_batch]))
                cached_batches_in_mem1 = cached_batches_in_mem1.vstack(next_batch)
            
            cached_batches_in_mem2 = cached_batches_in_mem2[disk_contrib2:]
            if len(cached_batches_in_mem2) < self.record_batch_rows and next_batch_to_get2 < number_of_batches_in_source2:
                next_batch = source2.get_batch(next_batch_to_get2)
                next_batch_to_get2 += 1
                next_batch = polars.from_arrow(pa.Table.from_batches([next_batch]))
                cached_batches_in_mem2 = cached_batches_in_mem2.vstack(next_batch)
            
            read_time += time.time() - start

        
        writer.close()
        print(read_time, write_time, sort_time)

    def done(self, executor_id):
        
        # first merge all of the in memory states to a file. This makes programming easier and likely not horrible in terms of performance. And we can save some memory! 
        # yolo and hope that that you can concatenate all and not die
        mem_idx = [i for i in range(len(self.states)) if type(self.states[i]) == polars.internals.frame.DataFrame]
        if len(mem_idx) > 0:
            in_mem_state = polars.concat([i for i in self.states if type(i) == polars.internals.frame.DataFrame]).sort(self.key)

            for ele in sorted(mem_idx, reverse = True):
                del self.states[ele]
            self.write_out_df_to_disk(self.data_dir + "/" + self.prefix + "-" + str(executor_id) + "-" + str(self.fileno) + ".arrow", in_mem_state)
            self.states.append(self.data_dir + "/" + self.prefix + "-" + str(executor_id) + "-" + str(self.fileno) + ".arrow")
            self.fileno += 1
            del in_mem_state

        # now all the states should be strs!
        assert sum([type(i) != str for i in self.states]) == 0
        print("MY DISK STATE", self.states)

        
        sources = [pa.ipc.open_file(pa.memory_map(k, 'rb')) for k in self.states]
        number_of_batches_in_sources = [source.num_record_batches for source in sources]
        next_batch_to_gets = [1 for i in self.states]
        

        del self.states
        gc.collect()
        import os, psutil
        process = psutil.Process(os.getpid())
        print("mem usage", process.memory_info().rss, pa.total_allocated_bytes())

        cached_batches_in_mem = [pa.Table.from_batches([source.get_batch(0)]) for source in sources]
        cached_batches_in_mem = [batch.append_column('asdasd', pa.array(np.ones(len(batch)) * j, pa.int32())) for batch, j in zip(cached_batches_in_mem, range(len(sources)))]

        while sum([len(i) != 0 for i in cached_batches_in_mem]) > 0:
        
            print("mem usage", process.memory_info().rss,  pa.total_allocated_bytes())
                
            disk_portions = [cached_batch_in_mem[:self.record_batch_rows].select([self.key, 'asdasd']) for cached_batch_in_mem in cached_batches_in_mem]

            temp = pa.concat_tables(disk_portions)
            new_batch = temp.take(compute.sort_indices(temp, sort_keys = [(self.key, "ascending")]))[:self.record_batch_rows]

            disk_contribs = [compute.sum(compute.equal(new_batch['asdasd'], j)).as_py() for j in range(len(disk_portions))]

            result = pa.concat_tables([cached_batches_in_mem[j][:disk_contribs[j]] for j in range(len(cached_batches_in_mem))])
            #result = result.take(compute.sort_indices(result, sort_keys = [(self.key, "ascending")]))
            #time.sleep(2)

            for j in range(len(cached_batches_in_mem)):
                cached_batches_in_mem[j] = cached_batches_in_mem[j][disk_contribs[j]:]
                
                #cached_batches_in_mem[j] = cached_batches_in_mem[j][1000:]#.copy()

                if len(cached_batches_in_mem[j]) < self.record_batch_rows and next_batch_to_gets[j] < number_of_batches_in_sources[j]:
                    next_batch = pa.Table.from_batches([sources[j].get_batch(next_batch_to_gets[j])])#.to_pandas()
                    next_batch = next_batch.append_column('asdasd', pa.array(np.ones(len(next_batch)) * j, pa.int32()))
                    next_batch_to_gets[j] += 1
                    cached_batches_in_mem[j] = pa.concat_tables([cached_batches_in_mem[j], next_batch])
                    del next_batch
            
            print(gc.collect())
            yield result
    
    # this is some crazy wierd algo that I came up with, might be there before.
    def execute(self, batches, stream_id, executor_id):
        print("NUMBER OF INCOMING BATCHES", len(batches))
        print("MY SORT STATE", [(type(i), len(i)) for i in self.states if type(i) == polars.internals.frame.DataFrame])
        import os, psutil
        process = psutil.Process(os.getpid())
        print("mem usage", process.memory_info().rss, pa.total_allocated_bytes())
        batch = polars.concat(batches).sort(self.key)
        print("LENGTH OF INCOMING BATCH", len(batch))
        if self.record_batch_rows is None:
            self.record_batch_rows = len(batch)

        if len(batch) > self.length_limit:
            self.write_out_df_to_disk(self.data_dir + "/" + self.prefix + "-" + str(executor_id) + "-" + str(self.fileno) + ".arrow", batch)
            self.states.append(self.data_dir + "/" + self.prefix + "-" + str(executor_id) + "-" + str(self.fileno) + ".arrow")
            self.fileno += 1
        elif sum([len(i) for i in self.states if type(i) == polars.internals.frame.DataFrame]) + len(batch) > self.length_limit:
            mega_batch = polars.concat([i for i in self.states if type(i) == polars.internals.frame.DataFrame] + [batch]).sort(self.key)
            self.write_out_df_to_disk(self.data_dir + "/" + self.prefix + "-" + str(executor_id) + "-" + str(self.fileno) + ".arrow", mega_batch)
            del mega_batch
            self.states = [i for i in self.states if type(i) == str]
            self.states.append(self.data_dir + "/" + self.prefix + "-" + str(executor_id) + "-" + str(self.fileno) + ".arrow")
            self.fileno += 1
        else:
            self.states.append(batch)

#stuff = []
exe = MergeSortedExecutor('0', length_limit=1000)
for k in range(100):
   item = polars.from_pandas(pd.DataFrame(np.random.normal(size=(random.randint(1, 2000),1000))))
   exe.execute([item], 0, 0)
da = exe.done(0)
for bump in da:
    pass

# exe = MergeSortedExecutor('0', 3000)
# a = polars.from_pandas(pd.DataFrame(np.random.normal(size=(10000,1000)))).sort('0')
# b = polars.from_pandas(pd.DataFrame(np.random.normal(size=(10000,1000)))).sort('0')

# exe.write_out_df_to_disk("file.arrow", a)
#exe = MergeSortedExecutor( "l_partkey", record_batch_rows = 1000000, length_limit = 1000000, file_prefix = "mergesort", output_line_limit = 1000000)
#exe.produce_sorted_file_from_two_sorted_files("/data/test.arrow","/data/mergesort-0-29.arrow","/data/mergesort-1-31.arrow")

# del a
# process = psutil.Process(os.getpid())
# print(process.memory_info().rss)
# exe.produce_sorted_file_from_sorted_file_and_in_memory("file2.arrow","file.arrow",b)
# exe.produce_sorted_file_from_two_sorted_files("file3.arrow","file2.arrow","file.arrow")
