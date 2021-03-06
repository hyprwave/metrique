Metrique
========

Python/MondoDB Information Platform and Data Warehouse

*Metrique helps you bring data, structured and unstructured into an 
intuitive, indexable data object collection that supported transparent
timebased snapshotting, advanced ad-hoc querying and is fully integrated 
with the scientific python computing stack.*

**Author:** "Chris Ward" <cward@redhat.com>

**Sources:** https://github.com/drpoovilleorg/metrique


Installation
------------

**MongoDB**
We assume you have a MongoDB server up and running. If it's running
binded to 127.0.0.1 with noauth = true and ssl = false, metrique
should find it automatically.

If it's not localhost, then you must remember to run `metrique-server-setup` after installing metrique.

**Metrique**
(optional) Install virtualenv and create a new virtual environment for metrique.

Then, install metrique. 

    python-pip install metrique

.. note::
     If you see 'gcc' error, try installing gcc and python-devel libraries first

.. note::
     If you see 'Connection reset by peer' error, try option: --use-mirrors

At this point, if you have default mongob setup and it's started, you 
should be ready to go. Otherwise, run metrique-server-config.py.

Run::
    
    $> metrique-server start [1|0] [1|0]

Where 1|0 for argv 0 and 1 are debug on and async on respectively


**Client**

Metrique offers a http api, with a convenient pyclient that understands that api.

If metrique server is running on anything other than 127.0.0.1, run `metrique-client-setup`.

Then, assuming your server is configured and metrique-server is running, launch ipython::

    IN  [1] from metrique.client import pyclient
    IN  [2] c = pyclient()
    IN  [3] c.ping()
    OUT [3] pong!

If all is well, the metriqe server should return your ping!

Now, you can run an example etl job, for example::

    IN  [4] c.admin.etl.extract("git_commit")

Then, analyse away::

    IN  [5] q = c.query.fetch('git_commit', 'author, committer_ts') 
    IN  [6] q.groupby(['author']).size().plot(kind='bar')
    OUT [6] <matplotlib.axes.AxesSubplot at 0x6f77ad0>
