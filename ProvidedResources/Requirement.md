You are [choose a team name!], an up-and-coming money printing company. You have been contracted by Big Casino to design and build a secure and fair slot machine. Your task is to create a system that meets Big Casino’s design specification requirements while being innovative and providing an easy-to-use user experience. Big Casino requires the following:

- Your slot machine should not be a physical machine, but an executable Golang software package. A CLI interface is suggested, but feel free to write a web front-end if desired.
- The system should primarily be written in Golang , but sub-components written in other languages are permissible. For example, if you need a database, consider SQLite for easy integration.
- You may use any available Golang libraries in your implementation.
- The slot machine must support three user groups: Big Casino Technicians, Big Gamble Commissioners and Players.

## Big Casino Technicians must be able to:

- Authenticate into a technician-only interface.
- Manage games (add or remove) from the playable interface
	- There should be a minimum of two different slot games to be played.
	- Each game should have multiple betting options ($1, $5, etc.).
	- At least one game should support multiple win conditions, such as diagonals, multiple rows, etc.
	- At least one game to allow for holds on wheels for the next spin.
	- All random number generation needs to be cryptographically secure.
- Implement system logging for auditing purposes.
- Run a system diagnostic to display the wins and losses for each game, the frequency of game play, and other relevant information.

## Big Gamble Commissioners must be able to do:

- Authenticate into a testing-only mode
- Analyze the statistics of each game type, the wins and losses, frequency of payout, and other relevant information.
- Test a game for x rounds, playing the specific game x times, and calculate the statistics for the test session, including the random seeds used for each round.

## Players must be able to do:

- Register a new account and manage the account
	- Connect the account to a bank account or debit/credit card
	- Deposit funds or withdraw funds to and from the bank account
	- Manage the password for the account
- Play one of the slot machine games
	- Bet money if their account has sufficient funds
	- Cash out of the game for funds to be added to their account
- View their playing statistics such as number of games played, highest payout, total lost, win percentage, etc.

## Tips for success

- Review the Pokémon Game Corner slots for ideas.
- Review Secure Random Number Generation for information about secure random generation.