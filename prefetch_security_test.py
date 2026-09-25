import sys, unittest
from pathlib import Path
from unittest.mock import patch
import nb_fetch, safe_fetch

class PrefetchTests(unittest.TestCase):
    def test_private_initial_url_rejected_without_container_write(self):
        records=[(2,1,6,'',('169.254.169.254',80))]
        with patch('safe_fetch.socket.getaddrinfo',return_value=records), patch('nb_fetch.subprocess.run') as run:
            with self.assertRaises(safe_fetch.FetchDenied):nb_fetch.prefetch_notebook('pb-test','/work','http://169.254.169.254/metadata')
            run.assert_not_called()

    def test_public_bytes_only_pass_on_stdin(self):
        payload=b'{"cells": []}'
        with patch('safe_fetch.get',return_value=safe_fetch.Response(200,payload)) as get, patch('nb_fetch.subprocess.run') as run:
            nb_fetch.prefetch_notebook('pb-test','/work','https://example.com/test.ipynb?private_token=secret')
            get.assert_called_once_with('https://example.com/test.ipynb?private_token=secret',timeout=45)
            args,kw=run.call_args
            self.assertEqual(kw['input'],payload)
            self.assertNotIn('private_token',str(args))
            self.assertNotIn('curl',str(args))
            self.assertTrue(kw['check'])

    def test_failed_fetch_has_no_unchecked_fallback(self):
        with patch('safe_fetch.get',side_effect=safe_fetch.FetchDenied('blocked')), patch('nb_fetch.subprocess.run') as run:
            with self.assertRaises(safe_fetch.FetchDenied):nb_fetch.prefetch_notebook('pb-test','/work','https://example.com/test.ipynb')
            run.assert_not_called()

if __name__=='__main__':unittest.main()
