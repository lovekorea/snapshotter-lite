import asyncio
import json
import multiprocessing
import resource
import signal
import time
from signal import SIGINT
from signal import SIGQUIT
from signal import SIGTERM

from eth_utils.address import to_checksum_address
from web3 import Web3

from snapshotter.processor_distributor import ProcessorDistributor
from snapshotter.settings.config import settings
from snapshotter.utils.default_logger import logger
from snapshotter.utils.exceptions import GenericExitOnSignal
from snapshotter.utils.file_utils import read_json_file
from snapshotter.utils.models.data_models import DailyTaskCompletedEvent
from snapshotter.utils.models.data_models import DayStartedEvent
from snapshotter.utils.models.data_models import EpochReleasedEvent
from snapshotter.utils.models.data_models import SnapshottersUpdatedEvent
from snapshotter.utils.rpc import get_event_sig_and_abi
from snapshotter.utils.rpc import RpcHelper


class EventDetectorProcess(multiprocessing.Process):

    def __init__(self, name, **kwargs):
        """
        Initializes the SystemEventDetector class.

        Args:
            name (str): The name of the process.
            **kwargs: Additional keyword arguments to be passed to the multiprocessing.Process class.

        Attributes:
            _shutdown_initiated (bool): A flag indicating whether shutdown has been initiated.
            _logger (logging.Logger): The logger instance.
            _last_processed_block (None): The last processed block.
            rpc_helper (RpcHelper): The RpcHelper instance.
            contract_abi (dict): The contract ABI.
            contract_address (str): The contract address.
            contract (web3.eth.Contract): The contract instance.
            event_sig (dict): The event signature.
            event_abi (dict): The event ABI.
        """
        multiprocessing.Process.__init__(self, name=name, **kwargs)
        self._shutdown_initiated = False
        self._logger = logger.bind(
            module=name,
        )

        self._last_processed_block = None
        self.rpc_helper = RpcHelper(settings.rpc)


        # event EpochReleased(uint256 indexed epochId, uint256 begin, uint256 end, uint256 timestamp);
        # event DayStartedEvent(uint256 dayId, uint256 timestamp);
        # event DailyTaskCompletedEvent(address snapshotterAddress, uint256 dayId, uint256 timestamp);

        EVENTS_ABI = {
            'EpochReleased': json.loads('''{
                        "anonymous": false,
                        "inputs": [
                            {
                            "indexed": false,
                            "name": "epochId",
                            "type": "uint256"
                            },
                            {
                            "indexed": false,
                            "name": "beginBlock",
                            "type": "uint256"
                            },
                            {
                            "indexed": false,
                            "name": "endBlock",
                            "type": "uint256"
                            },
                            {
                            "indexed": false,
                            "name": "timestamp",
                            "type": "uint256"
                            }
                        ],
                        "name": "EpochReleased",
                        "type": "event"
                        }
                                        '''
            )
        }
        EVENT_SIGS = {
            'EpochReleased': 'EpochReleased(uint256,uint256,uint256,uint256)',
            'allSnapshottersUpdated': 'allSnapshottersUpdated(address,bool)',
            'DayStartedEvent': 'DayStartedEvent(uint256,uint256)',
            'DailyTaskCompletedEvent': 'DailyTaskCompletedEvent(address,uint256,uint256)',

        }

        self.event_sig, self.event_abi = get_event_sig_and_abi(
            EVENT_SIGS,
            EVENTS_ABI,
        )

        self.processor_distributor = ProcessorDistributor()
        self._initialized = False

    async def init(self):
        await self.processor_distributor.init()

    async def get_events(self, from_block: int, to_block: int):
        """
        Retrieves events from the blockchain for the given block range and returns them as a list of tuples.
        Each tuple contains the event name and an object representing the event data.

        Args:
            from_block (int): The starting block number.
            to_block (int): The ending block number.

        Returns:
            List[Tuple[str, Any]]: A list of tuples, where each tuple contains the event name
            and an object representing the event data.
        """

        if not self._initialized:
            await self.init()
            self._initialized = True


        events = []

        log = {
            "args": {
                "begin": to_block - 9,
                "end": to_block,
                "epochId": 1,
                "timestamp": int(time.time()),
            },
            'event': 'EpochReleased' 
        }
        
        event = EpochReleasedEvent(
                    begin=log['args']['begin'],
                    end=log['args']['end'],
                    epochId=log['args']['epochId'],
                    timestamp=log['args']['timestamp'],
                )
        latest_epoch_id = max(1, log['args']['epochId'])

        events.append((log['event'], event))

         

        self._logger.info('Events: {}', events)
        return events

    def _generic_exit_handler(self, signum, sigframe):
        """
        Handles the generic exit signal and initiates shutdown.

        Args:
            signum (int): The signal number.
            sigframe (object): The signal frame.

        Raises:
            GenericExitOnSignal: If the shutdown is initiated.
        """
        if (
            signum in [SIGINT, SIGTERM, SIGQUIT] and
            not self._shutdown_initiated
        ):
            self._shutdown_initiated = True
            raise GenericExitOnSignal

    async def _detect_events(self):
        """
        Continuously detects events by fetching the current block and comparing it to the last processed block.
        If the last processed block is too far behind the current block, it processes the current block.
        """
        try:
            self._logger.debug('rpc helper init {}', self.rpc_helper._nodes)
            await self.rpc_helper.init()
            current_block = await self.rpc_helper.get_current_block()
            self._logger.info('Current block: {}', current_block)
            print(current_block, 'current block')

        except Exception as e:
            self._logger.opt(exception=True).error(
                (
                    'Unable to fetch current block, ERROR: {}, '
                    'sleeping for {} seconds.'
                ),
                e,
                settings.rpc.polling_interval,
            )

            await asyncio.sleep(settings.rpc.polling_interval)
            

        if not self._last_processed_block:
            self._last_processed_block = current_block - 1

        if self._last_processed_block:
            if current_block - self._last_processed_block >= 10:
                self._logger.warning(
                    'Last processed block is too far behind current block, '
                    'processing current block',
                )
                self._last_processed_block = current_block - 10

            # Get events from current block to last_processed_block
            try:
                events = await self.get_events(self._last_processed_block, current_block)
            except Exception as e:
                self._logger.opt(exception=True).error(
                    (
                        'Unable to fetch events from block {} to block {}, '
                        'ERROR: {}, sleeping for {} seconds.'
                    ),
                    self._last_processed_block + 1,
                    current_block,
                    e,
                    settings.rpc.polling_interval,
                )
                await asyncio.sleep(settings.rpc.polling_interval)
                

        else:

            self._logger.debug(
                'No last processed epoch found, processing current block',
            )

            try:
                events = await self.get_events(current_block, current_block)
            except Exception as e:
                self._logger.opt(exception=True).error(
                    (
                        'Unable to fetch events from block {} to block {}, '
                        'ERROR: {}, sleeping for {} seconds.'
                    ),
                    current_block,
                    current_block,
                    e,
                    settings.rpc.polling_interval,
                )
                await asyncio.sleep(settings.rpc.polling_interval)
                

        for event_type, event in events:
            self._logger.info(
                'Processing event: {}', event,
            )
            asyncio.ensure_future(
                self.processor_distributor.process_event(
                    event_type, event,
                ),
            )

        self._last_processed_block = current_block

        self._logger.info(
            'DONE: Processed blocks till {}',
            current_block,
        )
        self._logger.info(
            'Sleeping for {} seconds...',
            settings.rpc.polling_interval,
        )

    def run(self):
        """
        A class for detecting system events.

        Methods:
        --------
        run()
            Starts the event detection process.
        """
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(
            resource.RLIMIT_NOFILE,
            (settings.rlimit.file_descriptors, hard),
        )
        for signame in [signal.SIGINT, signal.SIGTERM, signal.SIGQUIT]:
            signal.signal(signame, self._generic_exit_handler)

        self.ev_loop = asyncio.get_event_loop()

        self.ev_loop.run_until_complete(
            self._detect_events(),
        )
        # Define ANSI escape code for green color
        green_color = "\033[92m"
        # Reset color
        reset_color = "\033[0m"
        # Unicode character for check mark
        check_mark = "\u2713"

        # Print the green check mark
        self._logger.info(f"{green_color}{check_mark}: 'All Runs successful'{reset_color}")



if __name__ == '__main__':
    event_detector = EventDetectorProcess('EventDetector')
    event_detector.run()
