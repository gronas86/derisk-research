import asyncio
import logging
from decimal import Decimal

from starknet_py.net.client_models import Event

from db.crud import DBConnector
from db.models import InterestRate
from handler_tools.api_connector import DeRiskAPIConnector
from handler_tools.constants import ProtocolIDs, TOKEN_MAPPING
from handlers.blockchain_call import NET
from handlers.helpers import InterestRateState

HASHSTACK_INTEREST_RATE_ADDRESS = "0x01b862c518939339b950d0d21a3d4cc8ead102d6270850ac8544636e558fab68"

HASHSTACK_ID = ProtocolIDs.HASHSTACK_V0.value

SECONDS_IN_YEAR = Decimal(365 * 24 * 60 * 60)


class HashstackV0InterestRate:
    """Class for calculating interest rates on the Hashstack V0 protocol."""

    PAGINATION_SIZE = 1000

    def __init__(self):
        """
        Initialize the HashstackV0InterestRate object.
        """
        self.api_connector = DeRiskAPIConnector()
        self.db_connector = DBConnector()
        self.last_block_data: InterestRate | None = self.db_connector.get_last_interest_rate_record_by_protocol_id(
            HASHSTACK_ID
        )
        self.events: list[dict] = []
        self._events_over: bool = False

    def _set_events(self, start_block: int, end_block: int) -> None:
        """
        Fetch events from the API, filter them by token and set them to the events attribute.
        Set flag that events are over if the result is an error.
        """
        if not isinstance(start_block, int) or not isinstance(end_block, int):
            logging.info("Invalid block numbers provided.")
            self.events.clear()
        self.events.clear()
        result = self.api_connector.get_data(
            HASHSTACK_INTEREST_RATE_ADDRESS,
            start_block,
            end_block,
        )
        if isinstance(result, dict):
            logging.info(f"Error while fetching events: {result.get('error', 'Unknown error')}")
            self.events = []
            self._events_over = True
            return
        self.events = result

    def _add_block_data(self, block_data: list[InterestRate], interest_rate_entry: InterestRate) -> None:
        """
        Add the interest rate entry to the block data and update last block data stored.
        :param block_data: list[InterestRate] - list of blocks with interest rates data.
        :param interest_rate_entry: InterestRate - interest rate entry to add.
        """
        block_data.append(interest_rate_entry)
        self.last_block_data = interest_rate_entry

    def calculate_interest_rates(self) -> list[InterestRate]:
        """
        Calculate the interest rates for provided events range.
        :return: list[InterestRate] - list of blocks with interest rates data.
        """
        if not self.events:
            return []
        percents_decimals_shift = Decimal("0.0001")
        blocks_data: list[InterestRate] = []
        interest_rate_state = InterestRateState(self.events[0]["block_number"], self.last_block_data)

        for index, event in enumerate(self.events):
            # If block number in event is different from previous, add block data
            if interest_rate_state.current_block != event["block_number"]:
                self._add_block_data(blocks_data, interest_rate_state.build_interest_rate_model(HASHSTACK_ID))

            # Get token name. Validate event `key_name` and token name.
            token_name = TOKEN_MAPPING.get(event["data"][0], "")
            if not token_name or event["key_name"] != "current_apr":
                interest_rate_state.current_block = event["block_number"]
                continue

            # Set initial timestamp values for the first token event
            if not self.last_block_data or interest_rate_state.token_timestamps[token_name] == 0:
                interest_rate_state.token_timestamps[token_name] = event["timestamp"]
                interest_rate_state.current_block = event["block_number"]
                continue

            # Get needed variables
            interest_rate_state.current_timestamp = event["timestamp"]
            seconds_passed = interest_rate_state.get_seconds_passed(token_name)
            borrow_apr_bps = Decimal(int(event["data"][1], 16))
            supply_apr_bps = Decimal(int(event["data"][2], 16))

            # Calculate interest rate for supply and borrow and convert to percents using the formula:
            # (apr * seconds_passed / seconds_in_year) / 10000
            current_collateral_change = (supply_apr_bps * seconds_passed / SECONDS_IN_YEAR) * percents_decimals_shift
            current_debt_change = (borrow_apr_bps * seconds_passed / SECONDS_IN_YEAR) * percents_decimals_shift
            interest_rate_state.update_state_cumulative_data(
                token_name, event["block_number"], current_collateral_change, current_debt_change
            )

        # Write last block data
        self._add_block_data(blocks_data, interest_rate_state.build_interest_rate_model(HASHSTACK_ID))
        return blocks_data

    def _get_blocks_bounds(self, start_block, end_block, latest_block) -> tuple[int, int]:
        """
        Calculate the bounds for the blocks pagination.
        :param start_block: int - Previous start block number.
        :param end_block: int - Previous end block number.
        :param latest_block: int - The latest block number.
        :return: tuple[int, int] - The new start and end block numbers.
        """
        if end_block + self.PAGINATION_SIZE < latest_block:
            start_block += end_block
        else:
            end_block = latest_block
            return start_block, end_block
        if start_block + self.PAGINATION_SIZE <= latest_block:
            end_block += self.PAGINATION_SIZE
        else:
            end_block = latest_block
        return start_block, end_block

    def run(self) -> None:
        """Run the interest rate calculation process from the last stored block or from 0 block."""
        latest_block = asyncio.run(NET.get_block_number())
        start_block = self.last_block_data.block if self.last_block_data else 0
        if start_block == latest_block:
            return
        start_block, end_block = self._get_blocks_bounds(
            start_block,
            start_block + self.PAGINATION_SIZE,
            latest_block
        )
        events_over = False
        # Fetch and set events until blocks are over
        while not events_over:
            if end_block >= latest_block:
                events_over = True
            self._set_events(start_block, end_block)
            start_block, end_block = self._get_blocks_bounds(start_block, end_block, latest_block)
            if not self.events:
                continue
            processed_data = self.calculate_interest_rates()
            self.db_connector.write_batch_to_db(processed_data)


if __name__ == "__main__":
    interest_rate = HashstackV0InterestRate()
    interest_rate.run()
