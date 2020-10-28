import mmap
import numpy as np
import io

import pyarrow as pa
import pyarrow.csv
from dask.utils import parse_bytes

import vaex.dataset
from vaex.dataset import Dataset
from .itertools import pmap, pwait, buffer, consume, filter_none
from .arrow.dataset import thread_count_default_io, get_main_io_pool


MB = 1024**2


def file_chunks(file, chunk_size, newline_readahead):
    """Bytes chunks, split by chunk_size bytes, on newline boundaries"""
    offset = 0
    with open(file, 'rb') as file:
        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)
        begin_offset = 0
    
        done = False
        while not done:
            # find the next newline boundary
            end_offset = min(file_size, begin_offset + chunk_size)
            done = end_offset == file_size
            if end_offset < file_size:
                file.seek(end_offset)
                sample = file.read(newline_readahead)
                offset = vaex.superutils.find_byte(sample, ord(b'\n'))
                if offset != -1:
                    end_offset += offset + 1 # include the newline
                else:
                    raise ValueError('expected newline')

            def reader(file_offset=begin_offset, length=end_offset - begin_offset):
                file_threadsafe = vaex.file.dup(file)
                file_threadsafe.seek(file_offset)
                return file_threadsafe.read(length)
            yield reader
            begin_offset = end_offset


def file_chunks_mmap(file, chunk_size, newline_readahead):
    """Bytes chunks, split by chunk_size bytes, on newline boundaries
    
    Using memory mapping (which avoids a memcpy)
    """
    offset = 0
    with open(file, 'rb') as file:
        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)
        begin_offset = 0
        kwargs = {}
        if vaex.utils.osname == "windows":
            kwargs["access"] = mmap.ACCESS_READ
        else:
            kwargs["prot"] = mmap.PROT_READ

        file_map = mmap.mmap(file.fileno(), file_size, **kwargs)
        data = memoryview(file_map)
    
        done = False
        while not done:
            # find the next newline boundary
            end_offset = min(file_size, begin_offset + chunk_size)
            if end_offset < file_size:
                sample = data[end_offset:end_offset+newline_readahead]
                offset = vaex.superutils.find_byte(sample, ord(b'\n'))
                if offset != -1:
                    end_offset += offset + 1 # include the newline
                else:
                    raise ValueError('expected newline')
            done = end_offset == file_size

            length = end_offset - begin_offset
            assert length > 0
            def reader(file_offset=begin_offset, length=length):
                return data[file_offset:file_offset+length]
            yield reader
            begin_offset = end_offset


def _row_count(chunk):
    ar = np.frombuffer(chunk, dtype=np.uint8)
    lines = vaex.superutils.count_byte(ar, ord(b'\n'))
    if ar[-1] != ord(b'\n'):
        lines += 1
    return lines


class DatasetCsv(Dataset):
    def __init__(self, file, chunk_size=10*MB, newline_readahead=1*MB, row_count=None):
        self.file = file
        self.chunk_size = parse_bytes(chunk_size)
        self.newline_readahead = parse_bytes(newline_readahead)
        reader = pyarrow.csv.open_csv(file)
        self._arrow_schema = reader.read_next_batch().schema
        self._schema = dict(zip(self._arrow_schema.names, self._arrow_schema.types))
        self._columns = {name: vaex.dataset.ColumnProxy(self, name, type) for name, type in
                          zip(self._arrow_schema.names, self._arrow_schema.types)}
        pool = get_main_io_pool()
        workers = pool._max_workers
        self._fragment_info = {}
        if row_count is None:
            chunks = file_chunks_mmap(file, self.chunk_size, self.newline_readahead)
            def process(i, chunk_reader):
                row_count = _row_count(chunk_reader())
                if i == 0:
                    row_count -= 1  # we counted the header (TODO: depends on ReadOptions)
                self._fragment_info[i] = dict(row_count=row_count)
            consume(pwait(buffer(pmap(process, enumerate(chunks), pool=pool), workers+3)))
            row_start = 0
            for i in range(len(self._fragment_info)):
                row_end = row_start + self._fragment_info[i]['row_count']
                self._fragment_info[i]['row_start'] = row_start
                self._fragment_info[i]['row_end'] = row_end
                row_start = row_end
            self._row_count = row_start
            pool.map(process, enumerate(chunks))
        else:
            self._row_count = row_count
        self._ids = {}

    def hashed(self):
        raise NotImplementedError

    def slice(self, start, end):
        # TODO: we can be smarter here, and trim off some fragments
        if start == 0 and end == self.row_count:
            return self
        return vaex.dataset.DatasetSliced(self, start=start, end=end)

    def is_masked(self, column):
        return False

    def shape(self, column):
        return tuple()

    def __getitem__(self, item):
        if isinstance(item, slice):
            assert item.step in [1, None]
            return vaex.dataset.DatasetSliced(self, item.start or 0, item.stop or self.row_count)
        return self._columns[item]

    def close(self):
        # no need to close it, it seem
        pass

    def _chunk_producer(self, columns, chunk_size=None, reverse=False, start=0, end=None):
        pool = get_main_io_pool()
        
        first = True
        previous = None
        for i, reader in enumerate(file_chunks_mmap(self.file, self.chunk_size, self.newline_readahead)):
            fragment_info = self._fragment_info.get(i)
            # bail out/continue early
            if fragment_info:
                if start >= fragment_info['row_end']:  # we didn't find the beginning yet
                    continue
                # TODO, not triggered, should be <=
                if end < fragment_info['row_start']:  # we are past the end
                    # assert False
                    break

            def chunk_reader(reader=reader, first=first, previous=previous, fragment_info=fragment_info, i=i):
                bytes = reader()
                file_like = pa.input_stream(bytes)
                use_threads = True
                block_size = len(bytes)
                if i == 0:
                    read_options = pyarrow.csv.ReadOptions(use_threads=use_threads, block_size=block_size)
                else:
                    read_options = pyarrow.csv.ReadOptions(use_threads=use_threads, column_names=list(self._schema.keys()), block_size=block_size)
                convert_options = pyarrow.csv.ConvertOptions(column_types=self._arrow_schema, include_columns=columns)
                table = pyarrow.csv.read_csv(file_like, read_options=read_options, convert_options=convert_options)


                row_count = len(table)
                row_start = 0
                if i not in self._fragment_info:
                    if previous:
                        row_start, row_end, chunks = previous.result()
                        row_start = row_end
                    row_end = row_start + len(table)
                    self._fragment_info[i] = dict(
                            # begin_offset=begin_offset,
                            # end_offset=end_offset,
                            row_count=row_count,
                            row_start=row_start,
                            row_end=row_end,
                        )
                else:
                    row_start = self._fragment_info[i]['row_start']
                    row_end = self._fragment_info[i]['row_end']

                # this is the bail out when we didn't have the fragments info cached yet
                fragment_info = self._fragment_info[i]
                if start >= fragment_info['row_end']:  # we didn't find the beginning yet
                    return None
                if end <= fragment_info['row_start']:  # we are past the end
                    return None
                # print(start, end, fragment_info, row_start, row_end)


                if start > row_start:
                    # this means we have to cut off a piece of the beginning
                    if end < row_end:
                        # AND the end
                        length = end - row_start  # without the start cut off
                        length -= start - row_start  # correcting for the start cut off
                        # print(start, end, length, row_start, row_end)
                        table = table.slice(start - row_start, length)
                    else:
                        table = table.slice(start - row_start)
                else:
                    if end < row_end:
                        # we only need to cut off a piece of the end
                        length = end - row_start
                        table = table.slice(0, length)
                
                # table = table.combine_chunks()
                assert len(table)
                chunks = dict(zip(table.column_names, table.columns))
                return row_start, row_end, chunks


            previous = pool.submit(chunk_reader)
            yield previous
            first = False

    def chunk_iterator(self, columns, chunk_size=None, reverse=False, start=0, end=None):
        chunk_size = chunk_size or 1024*1024
        i1 = 0
        chunks_ready_list = []
        i1 = i2 = 0

        # althoug arrow uses threading, we still manage to get some extra performance out of it with more threads
        chunk_generator = self._chunk_producer(columns, chunk_size, start=start, end=end or self._row_count)
        for c1, c2, chunks in filter_none(pwait(buffer(chunk_generator, thread_count_default_io//2+3))):
            chunks_ready_list.append(chunks)
            total_row_count = sum([len(list(k.values())[0]) for k in chunks_ready_list])
            if total_row_count > chunk_size:
                chunks_current_list, current_row_count = vaex.dataset._slice_of_chunks(chunks_ready_list, chunk_size)
                i2 += current_row_count
                yield i1, i2, vaex.dataset._concat_chunk_list(chunks_current_list)
                i1 = i2

        while chunks_ready_list:
            chunks_current_list, current_row_count = vaex.dataset._slice_of_chunks(chunks_ready_list, chunk_size)
            i2 += current_row_count
            yield i1, i2, vaex.dataset._concat_chunk_list(chunks_current_list)
            i1 = i2
