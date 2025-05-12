## Can I use different Python versions for the client and worker ?

Although you can install different python packages on your execution environment, you're constrained to a single python version on both your worker and client. There is currently no support for switching between different Python versions. If you're using the harmonic_snake worker then you need to use Python 3.10.12 for your client. 

## Working with larger objects

If you're working with objects that are bigger than 30mbs then you'll have to take care of chunking them on your own. This is because ArmoniK doesn't support objects that are bigger than 30mbs. 
