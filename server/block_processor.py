# See the file "LICENSE" for information about the copyright
# and warranty status of this software.

import array
import ast
import asyncio
import struct
import time
from collections import defaultdict
from functools import partial

import plyvel

from server.cache import FSCache, UTXOCache
from server.daemon import DaemonError
from lib.hash import hash_to_str
from lib.util import LoggedClass


def formatted_time(t):
    '''Return a number of seconds as a string in days, hours, mins and
    secs.'''
    t = int(t)
    return '{:d}d {:02d}h {:02d}m {:02d}s'.format(
        t // 86400, (t % 86400) // 3600, (t % 3600) // 60, t % 60)


class ChainError(Exception):
    pass


class Prefetcher(LoggedClass):
    '''Prefetches blocks (in the forward direction only).'''

    def __init__(self, daemon, height):
        super().__init__()
        self.daemon = daemon
        self.queue = asyncio.Queue()
        self.queue_semaphore = asyncio.Semaphore()
        self.queue_size = 0
        # Target cache size.  Has little effect on sync time.
        self.target_cache_size = 10 * 1024 * 1024
        self.fetched_height = height
        self.recent_sizes = [0]

    async def get_blocks(self):
        '''Returns a list of prefetched blocks.'''
        blocks, total_size = await self.queue.get()
        self.queue_size -= total_size
        return blocks

    async def start(self):
        '''Loops forever polling for more blocks.'''
        self.logger.info('prefetching blocks...')
        while True:
            while self.queue_size < self.target_cache_size:
                try:
                    await self._prefetch()
                except DaemonError as e:
                    self.logger.info('ignoring daemon errors: {}'.format(e))
            await asyncio.sleep(2)

    def _prefill_count(self, room):
        ave_size = sum(self.recent_sizes) // len(self.recent_sizes)
        count = room // ave_size if ave_size else 0
        return max(count, 10)

    async def _prefetch(self):
        '''Prefetch blocks if there are any to prefetch.'''
        daemon_height = await self.daemon.height()
        max_count = min(daemon_height - self.fetched_height, 4000)
        count = min(max_count, self._prefill_count(self.target_cache_size))
        first = self.fetched_height + 1
        hashes = await self.daemon.block_hex_hashes(first, count)
        if not hashes:
            return

        blocks = await self.daemon.raw_blocks(hashes)
        sizes = [len(block) for block in blocks]
        total_size = sum(sizes)
        self.queue.put_nowait((blocks, total_size))
        self.queue_size += total_size
        self.fetched_height += len(blocks)

        # Keep 50 most recent block sizes for fetch count estimation
        self.recent_sizes.extend(sizes)
        excess = len(self.recent_sizes) - 50
        if excess > 0:
            self.recent_sizes = self.recent_sizes[excess:]


class BlockProcessor(LoggedClass):
    '''Process blocks and update the DB state to match.

    Employ a prefetcher to prefetch blocks in batches for processing.
    Coordinate backing up in case of chain reorganisations.
    '''

    def __init__(self, env, daemon):
        super().__init__()

        self.daemon = daemon

        # Meta
        self.utxo_MB = env.utxo_MB
        self.hist_MB = env.hist_MB
        self.next_cache_check = 0
        self.last_flush = time.time()
        self.coin = env.coin

        # Chain state (initialize to genesis in case of new DB)
        self.db_height = -1
        self.db_tx_count = 0
        self.flush_count = 0
        self.utxo_flush_count = 0
        self.wall_time = 0
        self.tip = b'\0' * 32

        # Open DB and metadata files.  Record some of its state.
        self.db = self.open_db(self.coin)
        self.tx_count = self.db_tx_count
        self.height = self.db_height

        # Caches to be flushed later.  Headers and tx_hashes have one
        # entry per block
        self.history = defaultdict(partial(array.array, 'I'))
        self.history_size = 0
        self.utxo_cache = UTXOCache(self, self.db, self.coin)
        self.fs_cache = FSCache(self.coin, self.height, self.tx_count)
        self.prefetcher = Prefetcher(daemon, self.height)

        # Redirected member func
        self.get_tx_hash = self.fs_cache.get_tx_hash

        # Log state
        self.logger.info('{}/{} height: {:,d} tx count: {:,d} '
                         'flush count: {:,d} utxo flush count: {:,d} '
                         'sync time: {}'
                         .format(self.coin.NAME, self.coin.NET, self.height,
                                 self.tx_count, self.flush_count,
                                 self.utxo_flush_count,
                                 formatted_time(self.wall_time)))
        self.logger.info('flushing UTXO cache at {:,d} MB'
                         .format(self.utxo_MB))
        self.logger.info('flushing history cache at {:,d} MB'
                         .format(self.hist_MB))

    def coros(self):
        return [self.start(), self.prefetcher.start()]

    async def start(self):
        '''Loop forever processing blocks in the appropriate direction.'''
        try:
            while True:
                blocks = await self.prefetcher.get_blocks()
                for block in blocks:
                    self.process_block(block)
                    # Release asynchronous block fetching
                    await asyncio.sleep(0)

                if self.height == self.daemon.cached_height():
                    self.logger.info('caught up to height {:d}'
                                     .format(self_height))
                    self.flush(True)
        finally:
            if self.daemon.cached_height() is not None:
                self.flush(True)

    def open_db(self, coin):
        db_name = '{}-{}'.format(coin.NAME, coin.NET)
        try:
            db = plyvel.DB(db_name, create_if_missing=False,
                           error_if_exists=False, compression=None)
        except:
            db = plyvel.DB(db_name, create_if_missing=True,
                           error_if_exists=True, compression=None)
            self.logger.info('created new database {}'.format(db_name))
            self.flush_state(db)
        else:
            self.logger.info('successfully opened database {}'.format(db_name))
            self.read_state(db)
            self.delete_excess_history(db)

        return db

    def read_state(self, db):
        state = db.get(b'state')
        state = ast.literal_eval(state.decode())
        if state['genesis'] != self.coin.GENESIS_HASH:
            raise ChainError('DB genesis hash {} does not match coin {}'
                             .format(state['genesis_hash'],
                                     self.coin.GENESIS_HASH))
        self.db_height = state['height']
        self.db_tx_count = state['tx_count']
        self.tip = state['tip']
        self.flush_count = state['flush_count']
        self.utxo_flush_count = state['utxo_flush_count']
        self.wall_time = state['wall_time']

    def delete_excess_history(self, db):
        '''Clear history flushed since the most recent UTXO flush.'''
        utxo_flush_count = self.utxo_flush_count
        diff = self.flush_count - utxo_flush_count
        if diff == 0:
            return
        if diff < 0:
            raise ChainError('DB corrupt: flush_count < utxo_flush_count')

        self.logger.info('DB not shut down cleanly.  Scanning for most '
                         'recent {:,d} history flushes'.format(diff))
        prefix = b'H'
        unpack = struct.unpack
        keys = []
        for key, hist in db.iterator(prefix=prefix):
            flush_id, = unpack('>H', key[-2:])
            if flush_id > self.utxo_flush_count:
                keys.append(key)

        self.logger.info('deleting {:,d} history entries'.format(len(keys)))
        with db.write_batch(transaction=True) as batch:
            for key in keys:
                db.delete(key)
            self.utxo_flush_count = self.flush_count
            self.flush_state(batch)
        self.logger.info('deletion complete')

    def flush_state(self, batch):
        '''Flush chain state to the batch.'''
        now = time.time()
        self.wall_time += now - self.last_flush
        self.last_flush = now
        state = {
            'genesis': self.coin.GENESIS_HASH,
            'height': self.db_height,
            'tx_count': self.db_tx_count,
            'tip': self.tip,
            'flush_count': self.flush_count,
            'utxo_flush_count': self.utxo_flush_count,
            'wall_time': self.wall_time,
        }
        batch.put(b'state', repr(state).encode())

    def flush_utxos(self, batch):
        self.logger.info('flushing UTXOs: {:,d} txs and {:,d} blocks'
                         .format(self.tx_count - self.db_tx_count,
                                 self.height - self.db_height))
        self.utxo_cache.flush(batch)
        self.utxo_flush_count = self.flush_count
        self.db_tx_count = self.tx_count
        self.db_height = self.height

    def flush(self, flush_utxos=False):
        '''Flush out cached state.

        History is always flushed.  UTXOs are flushed if flush_utxos.'''
        flush_start = time.time()
        last_flush = self.last_flush

        # Write out the files to the FS before flushing to the DB.  If
        # the DB transaction fails, the files being too long doesn't
        # matter.  But if writing the files fails we do not want to
        # have updated the DB.
        tx_diff = self.fs_cache.flush(self.height, self.tx_count)

        with self.db.write_batch(transaction=True) as batch:
            # History first - fast and frees memory.  Flush state last
            # as it reads the wall time.
            self.flush_history(batch)
            if flush_utxos:
                self.flush_utxos(batch)
            self.flush_state(batch)
            self.logger.info('committing transaction...')

        # Update and put the wall time again - otherwise we drop the
        # time it took leveldb to commit the batch
        self.flush_state(self.db)

        flush_time = int(self.last_flush - flush_start)
        self.logger.info('flush #{:,d} to height {:,d} took {:,d}s'
                         .format(self.flush_count, self.height, flush_time))

        # Log handy stats
        daemon_height = self.daemon.cached_height()
        txs_per_sec = int(self.tx_count / self.wall_time)
        this_txs_per_sec = 1 + int(tx_diff / (self.last_flush - last_flush))
        if self.height > self.coin.TX_COUNT_HEIGHT:
            tx_est = (daemon_height - self.height) * self.coin.TX_PER_BLOCK
        else:
            tx_est = ((daemon_height - self.coin.TX_COUNT_HEIGHT)
                      * self.coin.TX_PER_BLOCK
                      + (self.coin.TX_COUNT - self.tx_count))

        self.logger.info('txs: {:,d}  tx/sec since genesis: {:,d}, '
                         'since last flush: {:,d}'
                         .format(self.tx_count, txs_per_sec, this_txs_per_sec))
        self.logger.info('sync time: {}  ETA: {}'
                         .format(formatted_time(self.wall_time),
                                 formatted_time(tx_est / this_txs_per_sec)))

    def flush_history(self, batch):
        self.logger.info('flushing history')

        # Drop any None entry
        self.history.pop(None, None)

        self.flush_count += 1
        flush_id = struct.pack('>H', self.flush_count)
        for hash168, hist in self.history.items():
            key = b'H' + hash168 + flush_id
            batch.put(key, hist.tobytes())

        self.logger.info('{:,d} history entries in {:,d} addrs'
                         .format(self.history_size, len(self.history)))

        self.history = defaultdict(partial(array.array, 'I'))
        self.history_size = 0

    def cache_sizes(self):
        '''Returns the approximate size of the cache, in MB.'''
        # Good average estimates based on traversal of subobjects and
        # requesting size from Python (see deep_getsizeof).  For
        # whatever reason Python O/S mem usage is typically +30% or
        # more, so we scale our already bloated object sizes.
        one_MB = int(1048576 / 1.3)
        utxo_cache_size = len(self.utxo_cache.cache) * 187
        db_cache_size = len(self.utxo_cache.db_cache) * 105
        hist_cache_size = len(self.history) * 180 + self.history_size * 4
        utxo_MB = (db_cache_size + utxo_cache_size) // one_MB
        hist_MB = hist_cache_size // one_MB

        self.logger.info('cache stats at height {:,d}  daemon height: {:,d}'
                         .format(self.height, self.daemon.cached_height()))
        self.logger.info('  entries: UTXO: {:,d}  DB: {:,d}  '
                         'hist addrs: {:,d}  hist size: {:,d}'
                         .format(len(self.utxo_cache.cache),
                                 len(self.utxo_cache.db_cache),
                                 len(self.history),
                                 self.history_size))
        self.logger.info('  size: {:,d}MB  (UTXOs {:,d}MB hist {:,d}MB)'
                         .format(utxo_MB + hist_MB, utxo_MB, hist_MB))
        return utxo_MB, hist_MB

    def process_block(self, block):
        # We must update the fs_cache before calling process_tx() as
        # it uses the fs_cache for tx hash lookup
        header, tx_hashes, txs = self.fs_cache.process_block(block)
        prev_hash, header_hash = self.coin.header_hashes(header)
        if prev_hash != self.tip:
            raise ChainError('trying to build header with prev_hash {} '
                             'on top of tip with hash {}'
                             .format(hash_to_str(prev_hash),
                                     hash_to_str(self.tip)))

        self.tip = header_hash
        self.height += 1
        for tx_hash, tx in zip(tx_hashes, txs):
            self.process_tx(tx_hash, tx)

        # Check if we're getting full and time to flush?
        now = time.time()
        if now > self.next_cache_check:
            self.next_cache_check = now + 60
            utxo_MB, hist_MB = self.cache_sizes()
            if utxo_MB >= self.utxo_MB or hist_MB >= self.hist_MB:
                self.flush(utxo_MB >= self.utxo_MB)

    def process_tx(self, tx_hash, tx):
        cache = self.utxo_cache
        tx_num = self.tx_count

        # Add the outputs as new UTXOs; spend the inputs
        hash168s = cache.add_many(tx_hash, tx_num, tx.outputs)
        if not tx.is_coinbase:
            for txin in tx.inputs:
                hash168s.add(cache.spend(txin.prevout))

        for hash168 in hash168s:
            self.history[hash168].append(tx_num)
        self.history_size += len(hash168s)

        self.tx_count += 1

    @staticmethod
    def resolve_limit(limit):
        if limit is None:
            return -1
        assert isinstance(limit, int) and limit >= 0
        return limit

    def get_history(self, hash168, limit=1000):
        '''Generator that returns an unpruned, sorted list of (tx_hash,
        height) tuples of transactions that touched the address,
        earliest in the blockchain first.  Includes both spending and
        receiving transactions.  By default yields at most 1000 entries.
        Set limit to None to get them all.
        '''
        limit = self.resolve_limit(limit)
        prefix = b'H' + hash168
        for key, hist in self.db.iterator(prefix=prefix):
            a = array.array('I')
            a.frombytes(hist)
            for tx_num in a:
                if limit == 0:
                    return
                yield self.get_tx_hash(tx_num)
                limit -= 1

    def get_balance(self, hash168):
        '''Returns the confirmed balance of an address.'''
        return sum(utxo.value for utxo in self.get_utxos(hash168, limit=None))

    def get_utxos(self, hash168, limit=1000):
        '''Generator that yields all UTXOs for an address sorted in no
        particular order.  By default yields at most 1000 entries.
        Set limit to None to get them all.
        '''
        limit = self.resolve_limit(limit)
        unpack = struct.unpack
        prefix = b'u' + hash168
        utxos = []
        for k, v in self.db.iterator(prefix=prefix):
            (tx_pos, ) = unpack('<H', k[-2:])

            for n in range(0, len(v), 12):
                if limit == 0:
                    return
                (tx_num, ) = unpack('<I', v[n:n+4])
                (value, ) = unpack('<Q', v[n+4:n+12])
                tx_hash, height = self.get_tx_hash(tx_num)
                yield UTXO(tx_num, tx_pos, tx_hash, height, value)
                limit -= 1

    def get_utxos_sorted(self, hash168):
        '''Returns all the UTXOs for an address sorted by height and
        position in the block.'''
        return sorted(self.get_utxos(hash168, limit=None))

    def get_current_header(self):
        '''Returns the current header as a dictionary.'''
        return self.fs_cache.encode_header(self.height)
