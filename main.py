import random
import re
import time
from pathlib import Path

from hexbytes import HexBytes
from solcx import compile_source, install_solc, set_solc_version
from web3 import Web3

import utils
from config import Config
from contracts import CONTRACTS
from logger import logger


def bridge_to_scroll(
    arbitrum_rpc_url: str,
    arbitrum_explorer_url: str,
    private_key: str,
    amount: float
) -> bool:
    web3 = Web3(
        Web3.HTTPProvider(
            arbitrum_rpc_url
        )
    )

    account = web3.eth.account.from_key(private_key)

    logger.info(f'Bridging {amount} ETH to Scroll for account {account.address}')

    amount_in_wei = Web3.to_wei(amount, 'ether')

    with open(Path(__file__).parent / 'abi' / 'Merkly.json') as file:
        merkly_abi = file.read()

    merkly_contract = web3.eth.contract(
        address='0x4Ae8CEBcCD7027820ba83188DFD73CCAD0A92806',
        abi=merkly_abi
    )

    suggested_fee = utils.suggest_gas_fees(chain_id=web3.eth.chain_id)

    if suggested_fee is None:
        suggested_fee = {
            'gasPrice': web3.eth.gas_price
        }

    destination_chain_id = 214

    adapter_params = HexBytes(
        '000200000000000000000000000000000000000000000000000000000000000' +
        '30d40000000000000000000000000000000000000000000000000000' +
        hex(amount_in_wei)[2:].rjust(13, '0') +
        account.address[2:]
    )

    send_fee = merkly_contract.functions.estimateSendFee(
        destination_chain_id,
        b'0x0',
        adapter_params
    ).call()

    txn = merkly_contract.functions.bridgeGas(
        destination_chain_id,
        account.address,
        adapter_params
    ).build_transaction({
        'chainId': web3.eth.chain_id,
        'nonce': web3.eth.get_transaction_count(account.address),
        'from': account.address,
        'gas': 0,
        **suggested_fee,
        'value': amount_in_wei + sum(send_fee)
    })

    try:
        txn['gas'] = web3.eth.estimate_gas(txn)
    except Exception as e:
        if 'insufficient funds' in str(e):
            logger.error(f'Insufficient funds to bridge ETH for account {account.address}')
            return False
        else:
            logger.error(f'Exception occured while estimating gas: {e}')
            return False

    signed_txn = account.sign_transaction(txn)

    txn_hash = web3.eth.send_raw_transaction(signed_txn.rawTransaction)

    logger.info(f'Transaction: {arbitrum_explorer_url}{txn_hash.hex()}')

    receipt = utils.wait_for_transaction_receipt(
        web3=web3.eth,
        txn_hash=txn_hash
    )

    if receipt and receipt['status'] == 1:
        logger.info(f'Successfully bridged {amount} ETH to Scroll for account {account.address}')
        return True
    else:
        logger.error(f'Failed to bridge ETH to Scroll for account {account.address}')
        return False


def deploy_random_contract(config: Config, private_key: str):
    web3 = Web3(
        Web3.HTTPProvider(
            config.scroll_rpc_url
        )
    )
    eth_web3 = Web3(
        Web3.HTTPProvider(
            config.eth_rpc_url
        )
    )

    account = web3.eth.account.from_key(private_key)

    logger.info(f'Deploying contract for account {account.address}')

    while True:
        try:
            gas_price = eth_web3.eth.gas_price
        except Exception as e:
            logger.warning(f'Failed to get gas price: {e}')
        else:
            gas_gwei = float(Web3.from_wei(gas_price, 'gwei'))
            if gas_gwei <= config.max_gwei:
                break
        logger.info('Gas price is too high, waiting for it to decrease')
        time.sleep(10)

    random_contract: str = random.choice(CONTRACTS)

    value_names = re.findall('{([\w_]+)}', random_contract)

    values_dict = {
        name: utils.random_string(min_len=config.min_length, max_len=config.max_length)
        for name in value_names
    }

    if random.randint(0, 1) == 1:
        values_dict['contract_name'] = values_dict['contract_name'].title()

    install_solc('0.8.2')
    set_solc_version('0.8.2')

    compiled_contract = compile_source(random_contract.format(**values_dict))
    contract_abi = compiled_contract[f'<stdin>:{values_dict["contract_name"]}']['abi']
    contract_bytecode = compiled_contract[f'<stdin>:{values_dict["contract_name"]}']['bin']

    contract = web3.eth.contract(
        abi=contract_abi,
        bytecode=contract_bytecode
    )

    txn = contract.constructor().build_transaction({
        'chainId': web3.eth.chain_id,
        'nonce': web3.eth.get_transaction_count(account.address),
        'from': account.address,
        'gas': 0,
        'gasPrice': gas_price
    })

    account_balance = web3.eth.get_balance(account.address)

    if account_balance < Web3.to_wei(config.bridge_amount, 'ether'):
        bridge_result = bridge_to_scroll(
            arbitrum_rpc_url=config.arbitrum_rpc_url,
            arbitrum_explorer_url=config.arbitrum_explorer_url,
            private_key=private_key,
            amount=config.bridge_amount
        )

        if not bridge_result:
            return False

        logger.info('Waiting for bridge to complete')

        while True:
            try:
                balance_after = web3.eth.get_balance(account.address)
            except Exception as e:
                logger.warning(f'Failed to get account balance: {e}')
            else:
                if balance_after > account_balance:
                    break
            time.sleep(10)

    try:
        txn['gas'] = web3.eth.estimate_gas(txn)
    except Exception as e:
        if 'insufficient funds' in str(e):
            logger.error(f'Insufficient funds to deploy contract for account {account.address}')
            return False
        else:
            logger.error(f'Exception occured while estimating gas: {e}')
            return False

    signed_txn = account.sign_transaction(txn)
    txn_hash = web3.eth.send_raw_transaction(signed_txn.rawTransaction)

    logger.info(f'Transaction: {config.scroll_explorer_url}{txn_hash.hex()}')

    receipt = utils.wait_for_transaction_receipt(
        web3=web3.eth,
        txn_hash=txn_hash
    )

    if receipt and receipt['status'] == 1:
        logger.info(f'Contract deployed at address {receipt["contractAddress"]}')
        return True
    else:
        logger.error(f'Failed to deploy contract for account {account.address}')
        return False


def main():
    private_keys = utils.load_accounts()
    config = Config.load()

    logger.info('Starting contract deployment')
    logger.info(f'Loaded {len(private_keys)} private keys')

    total_deployed = 0

    for index, private_key in enumerate(private_keys):
        total_deployed += deploy_random_contract(
            config=config,
            private_key=private_key
        )

        if index != len(private_keys) - 1:
            utils.random_sleep(
                min_sleep_time=config.min_sleep_time,
                max_sleep_time=config.max_sleep_time
            )

    logger.info(f'Deployed {total_deployed} contracts')

    total_failed = len(private_keys) - total_deployed

    if total_failed:
        logger.info(f'Failed to deploy {total_failed} contracts')


if __name__ == '__main__':
    main()
